// SPDX-License-Identifier: GPL-3.0-or-later
#include "solver.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#if defined(_OPENMP)
#  include <omp.h>
#endif

namespace ysc {
namespace {

Vec3 read_vec3(const float* values, int32_t index) {
    return {values[index * 3], values[index * 3 + 1], values[index * 3 + 2]};
}

void write_vec3(float* values, int32_t index, const Vec3& value) {
    values[index * 3] = value.x;
    values[index * 3 + 1] = value.y;
    values[index * 3 + 2] = value.z;
}

void validate_index(int32_t index, int32_t size, const char* label) {
    if (index < 0 || index >= size) {
        throw std::out_of_range(std::string(label) + " index is out of range");
    }
}

// Greedy graph colouring of constraints. Two constraints that share a vertex
// receive different colours so each colour class is vertex-disjoint and safe
// for OpenMP write-parallel projection.
template <typename VisitVertices>
ColorGroups colour_constraints(
    int32_t constraint_count,
    int32_t vertex_count,
    VisitVertices&& visit_vertices) {
    ColorGroups groups;
    if (constraint_count <= 0) {
        return groups;
    }
    std::vector<std::vector<int32_t>> colours_at_vertex(static_cast<size_t>(vertex_count));
    std::vector<int32_t> constraint_colour(static_cast<size_t>(constraint_count), -1);
    std::vector<uint8_t> used;
    for (int32_t index = 0; index < constraint_count; ++index) {
        used.assign(groups.size() + 1, 0);
        visit_vertices(index, [&](int32_t vertex) {
            for (const int32_t colour : colours_at_vertex[static_cast<size_t>(vertex)]) {
                if (colour >= 0 && static_cast<size_t>(colour) < used.size()) {
                    used[static_cast<size_t>(colour)] = 1;
                }
            }
        });
        int32_t colour = 0;
        while (static_cast<size_t>(colour) < used.size() && used[static_cast<size_t>(colour)] != 0) {
            ++colour;
        }
        if (static_cast<size_t>(colour) >= groups.size()) {
            groups.emplace_back();
            used.push_back(0);
        }
        constraint_colour[static_cast<size_t>(index)] = colour;
        groups[static_cast<size_t>(colour)].push_back(index);
        visit_vertices(index, [&](int32_t vertex) {
            colours_at_vertex[static_cast<size_t>(vertex)].push_back(colour);
        });
    }
    return groups;
}

}  // namespace

ysc_config default_config() {
    ysc_config config{};
    config.time_step = 1.0F / 240.0F;
    config.substeps = 8;
    config.iterations = 16;
    // Constant kinematic closure per substep (metres). Larger values sew
    // panels shut faster; capture still finishes the last 2 mm.
    // 28 mm/substep keeps shoulder/long seams strong (do not weaken for contact).
    config.seam_attraction_step = 0.028F;
    config.seam_capture_distance = 0.002F;
    config.stretch_relaxation = 1.0F;
    config.shear_relaxation = 0.02F;
    config.bend_relaxation = 0.02F;
    config.stretch_limit = 0.05F;
    config.maximum_position_correction = 0.005F;
    config.contact_thickness = 0.005F;
    config.contact_velocity_retention = 0.0F;
    return config;
}

Solver::Solver(const ysc_create_desc& desc, const ysc_config& config) : config_(config) {
    validate_config();
    if (desc.vertex_count <= 0 || desc.positions == nullptr) {
        throw std::invalid_argument("create descriptor has no vertex positions");
    }
    if (desc.seam_count < 0 || (desc.seam_count > 0 && desc.seams == nullptr)) {
        throw std::invalid_argument("create descriptor has invalid seam data");
    }
    if (
        desc.edge_count < 0 ||
        (desc.edge_count > 0 && (desc.edges == nullptr || desc.edge_rest_lengths == nullptr))) {
        throw std::invalid_argument("create descriptor has invalid material edge data");
    }
    if (
        desc.quad_count < 0 ||
        (desc.quad_count > 0 && (desc.quads == nullptr || desc.quad_rest_metrics == nullptr))) {
        throw std::invalid_argument("create descriptor has invalid material quad data");
    }
    if (
        desc.bend_count < 0 ||
        (desc.bend_count > 0 && (desc.bends == nullptr || desc.bend_rest_lengths == nullptr))) {
        throw std::invalid_argument("create descriptor has invalid material bend data");
    }
    if (
        desc.body_vertex_count < 0 || desc.body_face_count < 0 ||
        (desc.body_vertex_count > 0 && desc.body_positions == nullptr) ||
        (desc.body_face_count > 0 && desc.body_faces == nullptr)) {
        throw std::invalid_argument("create descriptor has invalid Body data");
    }

    vertices_.resize(static_cast<size_t>(desc.vertex_count));
    for (int32_t index = 0; index < desc.vertex_count; ++index) {
        Vertex& vertex = vertices_[static_cast<size_t>(index)];
        vertex.position = read_vec3(desc.positions, index);
        vertex.previous = vertex.position;
        vertex.velocity = desc.velocities != nullptr ? read_vec3(desc.velocities, index) : Vec3{};
        vertex.inverse_mass = desc.inverse_masses != nullptr ? desc.inverse_masses[index] : 1.0F;
        vertex.locked = desc.locked != nullptr && desc.locked[index] != 0;
        if (vertex.locked) {
            vertex.velocity = {};
        }
        if (
            !finite(vertex.position) || !finite(vertex.velocity) ||
            !std::isfinite(vertex.inverse_mass) || vertex.inverse_mass < 0.0F) {
            throw std::invalid_argument("create descriptor contains invalid vertex data");
        }
    }

    seams_.reserve(static_cast<size_t>(desc.seam_count));
    for (int32_t index = 0; index < desc.seam_count; ++index) {
        const int32_t a = desc.seams[index * 2];
        const int32_t b = desc.seams[index * 2 + 1];
        validate_index(a, desc.vertex_count, "seam vertex");
        validate_index(b, desc.vertex_count, "seam vertex");
        if (a == b) {
            throw std::invalid_argument("seam endpoints must be distinct");
        }
        const float initial_length =
            length(vertices_[static_cast<size_t>(b)].position - vertices_[static_cast<size_t>(a)].position);
        seams_.push_back({a, b, 0.0F, initial_length <= config_.seam_capture_distance});
    }

    edges_.reserve(static_cast<size_t>(desc.edge_count));
    for (int32_t index = 0; index < desc.edge_count; ++index) {
        const int32_t a = desc.edges[index * 2];
        const int32_t b = desc.edges[index * 2 + 1];
        const float rest_length = desc.edge_rest_lengths[index];
        validate_index(a, desc.vertex_count, "material edge vertex");
        validate_index(b, desc.vertex_count, "material edge vertex");
        if (a == b || !(rest_length > kEpsilon) || !std::isfinite(rest_length)) {
            throw std::invalid_argument("material edge has invalid rest data");
        }
        edges_.push_back({a, b, rest_length});
    }

    quads_.reserve(static_cast<size_t>(desc.quad_count));
    for (int32_t index = 0; index < desc.quad_count; ++index) {
        Quad quad{};
        for (int32_t corner = 0; corner < 4; ++corner) {
            quad.vertices[static_cast<size_t>(corner)] = desc.quads[index * 4 + corner];
            validate_index(
                quad.vertices[static_cast<size_t>(corner)], desc.vertex_count, "material quad vertex");
        }
        quad.rest_u_squared = desc.quad_rest_metrics[index * 3];
        quad.rest_v_squared = desc.quad_rest_metrics[index * 3 + 1];
        quad.rest_shear = desc.quad_rest_metrics[index * 3 + 2];
        if (
            !(quad.rest_u_squared > kEpsilon * kEpsilon) ||
            !(quad.rest_v_squared > kEpsilon * kEpsilon) ||
            !std::isfinite(quad.rest_u_squared) || !std::isfinite(quad.rest_v_squared) ||
            !std::isfinite(quad.rest_shear)) {
            throw std::invalid_argument("material quad has invalid rest metric");
        }
        quads_.push_back(quad);
    }

    bends_.reserve(static_cast<size_t>(desc.bend_count));
    for (int32_t index = 0; index < desc.bend_count; ++index) {
        Bend bend{};
        for (int32_t point = 0; point < 3; ++point) {
            bend.vertices[static_cast<size_t>(point)] = desc.bends[index * 3 + point];
            validate_index(
                bend.vertices[static_cast<size_t>(point)], desc.vertex_count, "material bend vertex");
        }
        bend.previous_rest_length = desc.bend_rest_lengths[index * 2];
        bend.next_rest_length = desc.bend_rest_lengths[index * 2 + 1];
        if (
            !(bend.previous_rest_length > kEpsilon) || !(bend.next_rest_length > kEpsilon) ||
            !std::isfinite(bend.previous_rest_length) || !std::isfinite(bend.next_rest_length)) {
            throw std::invalid_argument("material bend has invalid rest lengths");
        }
        bends_.push_back(bend);
    }

    body_positions_.reserve(static_cast<size_t>(desc.body_vertex_count));
    for (int32_t index = 0; index < desc.body_vertex_count; ++index) {
        const Vec3 value = read_vec3(desc.body_positions, index);
        if (!finite(value)) {
            throw std::invalid_argument("Body contains a non-finite vertex");
        }
        body_positions_.push_back(value);
    }
    body_faces_.reserve(static_cast<size_t>(desc.body_face_count));
    for (int32_t index = 0; index < desc.body_face_count; ++index) {
        Face face{
            desc.body_faces[index * 3],
            desc.body_faces[index * 3 + 1],
            desc.body_faces[index * 3 + 2],
        };
        for (const int32_t vertex : face) {
            validate_index(vertex, desc.body_vertex_count, "Body face vertex");
        }
        body_faces_.push_back(face);
    }

    contact_corrections_.resize(vertices_.size());
    contact_correction_counts_.resize(vertices_.size());
    contact_face_.resize(vertices_.size(), -1);
    seam_driven_.resize(vertices_.size());
    body_bvh_.build(body_positions_, body_faces_);
    build_color_groups();
#if defined(YSC_ENABLE_CUDA)
    init_material_cuda();
    init_body_contact_cuda();
#endif
    require_finite_state();
}

#if defined(YSC_ENABLE_CUDA)
void Solver::init_material_cuda() {
    material_cuda_.reset();
    if (!MaterialCuda::device_available()) {
        return;
    }
    cuda_pos_pack_.resize(vertices_.size() * 3);
    cuda_seam_captured_.resize(seams_.size());
    cuda_locked_pack_.resize(vertices_.size());
    cuda_inv_mass_pack_.resize(vertices_.size());
    for (size_t i = 0; i < vertices_.size(); ++i) {
        cuda_inv_mass_pack_[i] = vertices_[i].inverse_mass;
        cuda_locked_pack_[i] = vertices_[i].locked ? 1 : 0;
    }
    std::vector<MaterialCuda::Edge> edges;
    edges.reserve(edges_.size());
    for (const Edge& e : edges_) {
        edges.push_back({e.a, e.b, e.rest_length});
    }
    std::vector<MaterialCuda::Quad> quads;
    quads.reserve(quads_.size());
    for (const Quad& q : quads_) {
        quads.push_back({
            q.vertices[0],
            q.vertices[1],
            q.vertices[2],
            q.vertices[3],
            q.rest_u_squared,
            q.rest_v_squared,
            q.rest_shear,
        });
    }
    std::vector<MaterialCuda::Bend> bends;
    bends.reserve(bends_.size());
    for (const Bend& b : bends_) {
        bends.push_back({
            b.vertices[0],
            b.vertices[1],
            b.vertices[2],
            b.previous_rest_length,
            b.next_rest_length,
        });
    }
    std::vector<MaterialCuda::Seam> seams;
    seams.reserve(seams_.size());
    for (const Seam& s : seams_) {
        seams.push_back({s.a, s.b, s.target_length});
    }
    auto backend = std::make_unique<MaterialCuda>();
    if (!backend->init(
            static_cast<int32_t>(vertices_.size()),
            cuda_inv_mass_pack_.data(),
            cuda_locked_pack_.data(),
            edges,
            edge_colour_offsets_,
            edge_colour_indices_,
            quads,
            quad_colour_offsets_,
            quad_colour_indices_,
            bends,
            bend_colour_offsets_,
            bend_colour_indices_,
            seams,
            seam_colour_offsets_,
            seam_colour_indices_)) {
        return;
    }
    MaterialCuda::Params params{};
    params.stretch_relaxation = config_.stretch_relaxation;
    params.shear_relaxation = config_.shear_relaxation;
    params.bend_relaxation = config_.bend_relaxation;
    params.stretch_limit = config_.stretch_limit;
    params.maximum_position_correction = config_.maximum_position_correction;
    backend->set_params(params);
    material_cuda_ = std::move(backend);
}

bool Solver::cuda_materials_ready() const noexcept {
    return material_cuda_ && material_cuda_->ready();
}

void Solver::init_body_contact_cuda() {
    body_contact_cuda_.reset();
    if (!BodyContactCuda::device_available() || body_bvh_.empty()) {
        return;
    }
    // Reuse the same host pack buffers material CUDA may have allocated.
    if (cuda_pos_pack_.size() != vertices_.size() * 3) {
        cuda_pos_pack_.resize(vertices_.size() * 3);
    }
    if (cuda_locked_pack_.size() != vertices_.size()) {
        cuda_locked_pack_.resize(vertices_.size());
    }
    auto backend = std::make_unique<BodyContactCuda>();
    if (!backend->init(static_cast<int32_t>(vertices_.size()), body_bvh_)) {
        return;
    }
    body_contact_cuda_ = std::move(backend);
}

bool Solver::cuda_body_contact_ready() const noexcept {
    return body_contact_cuda_ && body_contact_cuda_->ready();
}

void Solver::project_body_contacts_cuda(
    float* external_positions,
    int32_t* external_locked,
    bool gather,
    bool download_hits) {
    if (!cuda_body_contact_ready()) {
        return;
    }
    constexpr float kCollisionSearchM = 0.04F;
    clear_contact_corrections();
    last_auto_candidate_count_ = body_contact_cuda_->project(
        external_positions,
        external_locked,
        kCollisionSearchM,
        config_.contact_thickness,
        gather);
    if (download_hits) {
        body_contact_cuda_->download_hits(contact_correction_counts_.data());
    }
}

void Solver::pack_positions_to_cuda_buffer() {
    for (size_t i = 0; i < vertices_.size(); ++i) {
        cuda_pos_pack_[i * 3 + 0] = vertices_[i].position.x;
        cuda_pos_pack_[i * 3 + 1] = vertices_[i].position.y;
        cuda_pos_pack_[i * 3 + 2] = vertices_[i].position.z;
        cuda_locked_pack_[i] = vertices_[i].locked ? 1 : 0;
    }
}

void Solver::unpack_positions_from_cuda_buffer() {
    for (size_t i = 0; i < vertices_.size(); ++i) {
        vertices_[i].position = {
            cuda_pos_pack_[i * 3 + 0],
            cuda_pos_pack_[i * 3 + 1],
            cuda_pos_pack_[i * 3 + 2],
        };
    }
}

void Solver::pack_seam_captured_to_cuda() {
    for (size_t i = 0; i < seams_.size(); ++i) {
        cuda_seam_captured_[i] = seams_[i].captured ? 1 : 0;
    }
}
#endif

void Solver::project_materials(bool reverse) {
    // CPU path only. CUDA advance keeps positions on-device across iterations.
    project_seams();
    project_quad_shear(reverse);
    project_bends(reverse);
    project_edges(reverse);
    project_edges(!reverse);
}

void Solver::flatten_colors(
    const ColorGroups& groups,
    std::vector<int32_t>& offsets,
    std::vector<int32_t>& indices) {
    offsets.clear();
    indices.clear();
    offsets.reserve(groups.size() + 1);
    offsets.push_back(0);
    for (const std::vector<int32_t>& group : groups) {
        indices.insert(indices.end(), group.begin(), group.end());
        offsets.push_back(static_cast<int32_t>(indices.size()));
    }
}

void Solver::build_color_groups() {
    const int32_t vertex_count = static_cast<int32_t>(vertices_.size());
    seam_colors_ = colour_constraints(
        static_cast<int32_t>(seams_.size()),
        vertex_count,
        [&](int32_t index, auto&& emit) {
            emit(seams_[static_cast<size_t>(index)].a);
            emit(seams_[static_cast<size_t>(index)].b);
        });
    edge_colors_ = colour_constraints(
        static_cast<int32_t>(edges_.size()),
        vertex_count,
        [&](int32_t index, auto&& emit) {
            emit(edges_[static_cast<size_t>(index)].a);
            emit(edges_[static_cast<size_t>(index)].b);
        });
    quad_colors_ = colour_constraints(
        static_cast<int32_t>(quads_.size()),
        vertex_count,
        [&](int32_t index, auto&& emit) {
            for (const int32_t vertex : quads_[static_cast<size_t>(index)].vertices) {
                emit(vertex);
            }
        });
    bend_colors_ = colour_constraints(
        static_cast<int32_t>(bends_.size()),
        vertex_count,
        [&](int32_t index, auto&& emit) {
            for (const int32_t vertex : bends_[static_cast<size_t>(index)].vertices) {
                emit(vertex);
            }
        });
    flatten_colors(seam_colors_, seam_colour_offsets_, seam_colour_indices_);
    flatten_colors(edge_colors_, edge_colour_offsets_, edge_colour_indices_);
    flatten_colors(quad_colors_, quad_colour_offsets_, quad_colour_indices_);
    flatten_colors(bend_colors_, bend_colour_offsets_, bend_colour_indices_);
}

int32_t Solver::vertex_count() const noexcept {
    return static_cast<int32_t>(vertices_.size());
}

int32_t Solver::seam_count() const noexcept {
    return static_cast<int32_t>(seams_.size());
}

void Solver::validate_config() const {
    if (
        !(config_.time_step > 0.0F) || !std::isfinite(config_.time_step) ||
        config_.substeps <= 0 || config_.iterations <= 0 ||
        !(config_.seam_attraction_step > 0.0F) || !std::isfinite(config_.seam_attraction_step) ||
        !(config_.seam_capture_distance > 0.0F) || !std::isfinite(config_.seam_capture_distance) ||
        config_.stretch_relaxation < 0.0F || config_.stretch_relaxation > 1.0F ||
        !std::isfinite(config_.stretch_relaxation) ||
        config_.shear_relaxation < 0.0F || config_.shear_relaxation > 1.0F ||
        !std::isfinite(config_.shear_relaxation) ||
        config_.bend_relaxation < 0.0F || config_.bend_relaxation > 1.0F ||
        !std::isfinite(config_.bend_relaxation) ||
        config_.stretch_limit < 0.0F || !std::isfinite(config_.stretch_limit) ||
        !(config_.maximum_position_correction > 0.0F) ||
        !std::isfinite(config_.maximum_position_correction) ||
        !(config_.contact_thickness > 0.0F) || !std::isfinite(config_.contact_thickness) ||
        config_.contact_velocity_retention < 0.0F || config_.contact_velocity_retention > 1.0F ||
        !std::isfinite(config_.contact_velocity_retention)) {
        throw std::invalid_argument("solver configuration contains an invalid active value");
    }
}

void Solver::replace_state(
    const float* positions,
    const float* velocities,
    const int32_t* locked) {
    if (positions == nullptr || velocities == nullptr || locked == nullptr) {
        throw std::invalid_argument("replacement state pointer is null");
    }
    for (int32_t index = 0; index < vertex_count(); ++index) {
        Vertex& vertex = vertices_[static_cast<size_t>(index)];
        vertex.position = read_vec3(positions, index);
        vertex.previous = vertex.position;
        vertex.velocity = read_vec3(velocities, index);
        vertex.locked = locked[index] != 0;
        if (vertex.locked) {
            vertex.velocity = {};
        }
    }
    require_finite_state();
}

void Solver::copy_state(float* positions, float* velocities) const {
    if (positions == nullptr || velocities == nullptr) {
        throw std::invalid_argument("state output pointer is null");
    }
    for (int32_t index = 0; index < vertex_count(); ++index) {
        const Vertex& vertex = vertices_[static_cast<size_t>(index)];
        write_vec3(positions, index, vertex.position);
        write_vec3(velocities, index, vertex.velocity);
    }
}

void Solver::replace_seam_state(const float* target_lengths) {
    if (target_lengths == nullptr && !seams_.empty()) {
        throw std::invalid_argument("seam state input pointer is null");
    }
    for (size_t index = 0; index < seams_.size(); ++index) {
        const float value = target_lengths[index];
        if (!std::isfinite(value) || std::abs(value) > kEpsilon) {
            throw std::invalid_argument("seam targets are fixed at zero length");
        }
        seams_[index].target_length = 0.0F;
    }
}

void Solver::copy_seam_state(float* target_lengths) const {
    if (target_lengths == nullptr && !seams_.empty()) {
        throw std::invalid_argument("seam state output pointer is null");
    }
    for (size_t index = 0; index < seams_.size(); ++index) {
        target_lengths[index] = seams_[index].target_length;
    }
}

void Solver::integrate(const Vec3& gravity, float time_step) {
    const int32_t count = static_cast<int32_t>(vertices_.size());
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static)
#endif
    for (int32_t index = 0; index < count; ++index) {
        Vertex& vertex = vertices_[static_cast<size_t>(index)];
        vertex.previous = vertex.position;
        if (vertex.locked || vertex.inverse_mass <= 0.0F) {
            vertex.velocity = {};
            continue;
        }
        vertex.velocity += time_step * gravity;
        vertex.position += time_step * vertex.velocity;
    }
}

void Solver::project_seam_attraction() {
    // Sewing drags the panel kinematically; it is the operator's intent, not a
    // force.  Neither this pull nor the material's reaction to it may become
    // momentum, or the pair accelerates itself across the substeps.
    std::fill(seam_driven_.begin(), seam_driven_.end(), 0);
    const int32_t seam_count = static_cast<int32_t>(seams_.size());
    // Seams are few and usually vertex-disjoint after colouring, but attraction
    // runs once per substep serially so seam_driven_ writes stay simple.
    for (int32_t index = 0; index < seam_count; ++index) {
        const Seam& seam = seams_[static_cast<size_t>(index)];
        if (seam.captured) {
            continue;
        }
        seam_driven_[static_cast<size_t>(seam.a)] = 1;
        seam_driven_[static_cast<size_t>(seam.b)] = 1;
        Vertex& a = vertices_[static_cast<size_t>(seam.a)];
        Vertex& b = vertices_[static_cast<size_t>(seam.b)];
        const float a_weight = (!a.locked && a.inverse_mass > 0.0F) ? a.inverse_mass : 0.0F;
        const float b_weight = (!b.locked && b.inverse_mass > 0.0F) ? b.inverse_mass : 0.0F;
        const float weight_sum = a_weight + b_weight;
        if (!(weight_sum > 0.0F)) {
            continue;
        }
        const Vec3 difference = b.position - a.position;
        const float current_length = length(difference);
        if (!(current_length > kEpsilon)) {
            continue;
        }
        // A fixed closure keeps the rate independent of how far apart the pair
        // still is; never step past the pair, the capture test shuts the rest.
        const float closure = std::min(config_.seam_attraction_step, current_length);
        const Vec3 direction = difference / current_length;
        a.position += (a_weight / weight_sum * closure) * direction;
        b.position -= (b_weight / weight_sum * closure) * direction;
    }
}

void Solver::update_seam_capture() {
    for (Seam& seam : seams_) {
        if (seam.captured) {
            continue;
        }
        const Vertex& a = vertices_[static_cast<size_t>(seam.a)];
        const Vertex& b = vertices_[static_cast<size_t>(seam.b)];
        const Vec3 current_difference = b.position - a.position;
        const Vec3 previous_difference = b.previous - a.previous;
        if (
            length(current_difference) <= config_.seam_capture_distance ||
            dot(current_difference, previous_difference) <= 0.0F) {
            seam.captured = true;
        }
    }
}

void Solver::project_distance(
    int32_t a_index,
    int32_t b_index,
    float target_length,
    float relaxation) {
    if (!(relaxation > 0.0F)) {
        return;
    }
    Vertex& a = vertices_[static_cast<size_t>(a_index)];
    Vertex& b = vertices_[static_cast<size_t>(b_index)];
    const float a_weight = (!a.locked && a.inverse_mass > 0.0F) ? a.inverse_mass : 0.0F;
    const float b_weight = (!b.locked && b.inverse_mass > 0.0F) ? b.inverse_mass : 0.0F;
    const float weight_sum = a_weight + b_weight;
    if (!(weight_sum > 0.0F)) {
        return;
    }
    const Vec3 difference = b.position - a.position;
    const float current_length = length(difference);
    if (!(current_length > kEpsilon)) {
        return;
    }
    const Vec3 direction = difference / current_length;
    const float scaled_error = relaxation * (current_length - target_length) / weight_sum;
    if (a_weight > 0.0F) {
        a.position += clamp_length(a_weight * scaled_error * direction, config_.maximum_position_correction);
    }
    if (b_weight > 0.0F) {
        b.position -= clamp_length(b_weight * scaled_error * direction, config_.maximum_position_correction);
    }
}

void Solver::project_seams() {
    const int32_t colour_count = static_cast<int32_t>(seam_colour_offsets_.size()) - 1;
    if (colour_count <= 0) {
        return;
    }
#if defined(_OPENMP)
#  pragma omp parallel
#endif
    {
        for (int32_t colour = 0; colour < colour_count; ++colour) {
            const int32_t begin = seam_colour_offsets_[static_cast<size_t>(colour)];
            const int32_t end = seam_colour_offsets_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
#if defined(_OPENMP)
#  pragma omp for schedule(static) nowait
#endif
            for (int32_t local = 0; local < count; ++local) {
                const Seam& seam =
                    seams_[static_cast<size_t>(seam_colour_indices_[static_cast<size_t>(begin + local)])];
                if (seam.captured) {
                    project_distance(seam.a, seam.b, seam.target_length, 1.0F);
                }
            }
#if defined(_OPENMP)
#  pragma omp barrier
#endif
        }
    }
}

void Solver::project_edge(const Edge& edge) {
    Vertex& a = vertices_[static_cast<size_t>(edge.a)];
    Vertex& b = vertices_[static_cast<size_t>(edge.b)];
    const Vec3 difference =
        b.position - a.position;
    const float current_length = length(difference);
    // Both directions.  A yarn does not elongate, and the centimetre between two
    // crossings does not shorten either: cloth folds by bending the lattice out
    // of plane, with its cells still a centimetre across.  Letting a span
    // collapse instead makes compression a one-way ratchet that no later pass
    // can undo, and the panel silently loses its authored dimensions.
    const float slack = edge.rest_length * config_.stretch_limit;
    const bool beyond_crimp_reserve =
        current_length > edge.rest_length + slack || current_length < edge.rest_length - slack;
    // Always aim at the rest length; only the firmness changes.  Aiming at the
    // reserve bound instead would leave a span just past it stretched further
    // than one just inside it.
    const float relaxation = beyond_crimp_reserve ? 1.0F : config_.stretch_relaxation;
    if (!(relaxation > 0.0F) || !(current_length > kEpsilon)) {
        return;
    }
    const float a_weight = (!a.locked && a.inverse_mass > 0.0F) ? a.inverse_mass : 0.0F;
    const float b_weight = (!b.locked && b.inverse_mass > 0.0F) ? b.inverse_mass : 0.0F;
    const float weight_sum = a_weight + b_weight;
    if (!(weight_sum > 0.0F)) {
        return;
    }
    const Vec3 direction = difference / current_length;
    const float scaled_error =
        relaxation * (current_length - edge.rest_length) / weight_sum;
    if (a_weight > 0.0F) {
        a.position += clamp_length(
            a_weight * scaled_error * direction,
            config_.maximum_position_correction);
    }
    if (b_weight > 0.0F) {
        b.position -= clamp_length(
            b_weight * scaled_error * direction,
            config_.maximum_position_correction);
    }
}

void Solver::project_edges(bool reverse) {
    const int32_t colour_count = static_cast<int32_t>(edge_colour_offsets_.size()) - 1;
    if (colour_count <= 0) {
        return;
    }
    // Keep one OpenMP team across colours to avoid repeated fork/join on MSVC.
#if defined(_OPENMP)
#  pragma omp parallel
#endif
    {
        for (int32_t colour_offset = 0; colour_offset < colour_count; ++colour_offset) {
            const int32_t colour = reverse ? (colour_count - 1 - colour_offset) : colour_offset;
            const int32_t begin = edge_colour_offsets_[static_cast<size_t>(colour)];
            const int32_t end = edge_colour_offsets_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
#if defined(_OPENMP)
#  pragma omp for schedule(static)
#endif
            for (int32_t local = 0; local < count; ++local) {
                const int32_t index = reverse ? (count - 1 - local) : local;
                project_edge(edges_[static_cast<size_t>(
                    edge_colour_indices_[static_cast<size_t>(begin + index)])]);
            }
        }
    }
}

void Solver::project_quad(const Quad& quad) {
    std::array<Vertex*, 4> vertices{};
    std::array<float, 4> weights{};
    for (size_t corner = 0; corner < 4; ++corner) {
        vertices[corner] = &vertices_[static_cast<size_t>(quad.vertices[corner])];
        const Vertex& vertex = *vertices[corner];
        weights[corner] = (!vertex.locked && vertex.inverse_mass > 0.0F)
            ? vertex.inverse_mass
            : 0.0F;
    }
    const Vec3& x0 = vertices[0]->position;
    const Vec3& x1 = vertices[1]->position;
    const Vec3& x2 = vertices[2]->position;
    const Vec3& x3 = vertices[3]->position;
    const Vec3 u = 0.5F * ((x1 - x0) + (x2 - x3));
    const Vec3 v = 0.5F * ((x3 - x0) + (x2 - x1));
    const float value = dot(u, v) - quad.rest_shear;
    const std::array<Vec3, 4> gradients{
        -0.5F * (u + v),
        0.5F * (v - u),
        0.5F * (u + v),
        0.5F * (u - v),
    };
    float denominator = 0.0F;
    for (size_t corner = 0; corner < 4; ++corner) {
        denominator += weights[corner] * length_squared(gradients[corner]);
    }
    if (!(denominator > kEpsilon * kEpsilon)) {
        return;
    }
    const float multiplier = -config_.shear_relaxation * value / denominator;
    for (size_t corner = 0; corner < 4; ++corner) {
        if (weights[corner] > 0.0F) {
            vertices[corner]->position += clamp_length(
                weights[corner] * multiplier * gradients[corner],
                config_.maximum_position_correction);
        }
    }
}

void Solver::project_quad_shear(bool reverse) {
    const int32_t colour_count = static_cast<int32_t>(quad_colour_offsets_.size()) - 1;
    if (colour_count <= 0) {
        return;
    }
#if defined(_OPENMP)
#  pragma omp parallel
#endif
    {
        for (int32_t colour_offset = 0; colour_offset < colour_count; ++colour_offset) {
            const int32_t colour = reverse ? (colour_count - 1 - colour_offset) : colour_offset;
            const int32_t begin = quad_colour_offsets_[static_cast<size_t>(colour)];
            const int32_t end = quad_colour_offsets_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
#if defined(_OPENMP)
#  pragma omp for schedule(static)
#endif
            for (int32_t local = 0; local < count; ++local) {
                const int32_t index = reverse ? (count - 1 - local) : local;
                project_quad(quads_[static_cast<size_t>(
                    quad_colour_indices_[static_cast<size_t>(begin + index)])]);
            }
        }
    }
}

void Solver::project_bend(const Bend& bend) {
    std::array<Vertex*, 3> vertices{};
    std::array<float, 3> weights{};
    for (size_t point = 0; point < 3; ++point) {
        vertices[point] = &vertices_[static_cast<size_t>(bend.vertices[point])];
        const Vertex& vertex = *vertices[point];
        weights[point] = (!vertex.locked && vertex.inverse_mass > 0.0F)
            ? vertex.inverse_mass
            : 0.0F;
    }
    const float previous_coefficient = 1.0F / bend.previous_rest_length;
    const float next_coefficient = 1.0F / bend.next_rest_length;
    const std::array<float, 3> coefficients{
        previous_coefficient,
        -(previous_coefficient + next_coefficient),
        next_coefficient,
    };
    const Vec3 curvature =
        coefficients[0] * vertices[0]->position +
        coefficients[1] * vertices[1]->position +
        coefficients[2] * vertices[2]->position;
    float denominator = 0.0F;
    for (size_t point = 0; point < 3; ++point) {
        denominator += weights[point] * coefficients[point] * coefficients[point];
    }
    if (!(denominator > kEpsilon)) {
        return;
    }
    for (size_t point = 0; point < 3; ++point) {
        if (weights[point] > 0.0F) {
            vertices[point]->position += clamp_length(
                (-config_.bend_relaxation * weights[point] * coefficients[point] / denominator) *
                    curvature,
                config_.maximum_position_correction);
        }
    }
}

void Solver::project_bends(bool reverse) {
    const int32_t colour_count = static_cast<int32_t>(bend_colour_offsets_.size()) - 1;
    if (colour_count <= 0) {
        return;
    }
#if defined(_OPENMP)
#  pragma omp parallel
#endif
    {
        for (int32_t colour_offset = 0; colour_offset < colour_count; ++colour_offset) {
            const int32_t colour = reverse ? (colour_count - 1 - colour_offset) : colour_offset;
            const int32_t begin = bend_colour_offsets_[static_cast<size_t>(colour)];
            const int32_t end = bend_colour_offsets_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
#if defined(_OPENMP)
#  pragma omp for schedule(static)
#endif
            for (int32_t local = 0; local < count; ++local) {
                const int32_t index = reverse ? (count - 1 - local) : local;
                project_bend(bends_[static_cast<size_t>(
                    bend_colour_indices_[static_cast<size_t>(begin + index)])]);
            }
        }
    }
}

Vec3 Solver::closest_triangle_point(
    const Vec3& point,
    const Vec3& a,
    const Vec3& b,
    const Vec3& c) const {
    const Vec3 ab = b - a;
    const Vec3 ac = c - a;
    const Vec3 ap = point - a;
    const float d1 = dot(ab, ap);
    const float d2 = dot(ac, ap);
    if (d1 <= 0.0F && d2 <= 0.0F) {
        return a;
    }
    const Vec3 bp = point - b;
    const float d3 = dot(ab, bp);
    const float d4 = dot(ac, bp);
    if (d3 >= 0.0F && d4 <= d3) {
        return b;
    }
    const float vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0F && d1 >= 0.0F && d3 <= 0.0F) {
        return a + (d1 / (d1 - d3)) * ab;
    }
    const Vec3 cp = point - c;
    const float d5 = dot(ab, cp);
    const float d6 = dot(ac, cp);
    if (d6 >= 0.0F && d5 <= d6) {
        return c;
    }
    const float vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0F && d2 >= 0.0F && d6 <= 0.0F) {
        return a + (d2 / (d2 - d6)) * ac;
    }
    const float va = d3 * d6 - d5 * d4;
    if (va <= 0.0F && (d4 - d3) >= 0.0F && (d5 - d6) >= 0.0F) {
        return b + ((d4 - d3) / ((d4 - d3) + (d5 - d6))) * (c - b);
    }
    const float inverse = 1.0F / (va + vb + vc);
    return a + (vb * inverse) * ab + (vc * inverse) * ac;
}

void Solver::clear_contact_corrections() {
    std::fill(contact_corrections_.begin(), contact_corrections_.end(), Vec3{});
    std::fill(contact_correction_counts_.begin(), contact_correction_counts_.end(), 0);
}

void Solver::project_body_contacts_external(const int32_t* candidates, int32_t count) {
    // External candidates may list the same cloth vertex more than once; keep
    // accumulation serial for that path. Auto contacts use the parallel path.
    clear_contact_corrections();
    if (count <= 0) {
        return;
    }
    if (candidates == nullptr) {
        throw std::invalid_argument("Body candidate pointer is null");
    }
    for (int32_t index = 0; index < count; ++index) {
        const int32_t vertex_index = candidates[index * 2];
        const int32_t face_index = candidates[index * 2 + 1];
        Vertex& vertex = vertices_[static_cast<size_t>(vertex_index)];
        if (vertex.locked) {
            continue;
        }
        const Face& face = body_faces_[static_cast<size_t>(face_index)];
        const Vec3& a = body_positions_[static_cast<size_t>(face[0])];
        const Vec3& b = body_positions_[static_cast<size_t>(face[1])];
        const Vec3& c = body_positions_[static_cast<size_t>(face[2])];
        const Vec3 normal = normalized(cross(b - a, c - a));
        const Vec3 closest = closest_triangle_point(vertex.position, a, b, c);
        const float signed_distance = dot(vertex.position - closest, normal);
        if (signed_distance < config_.contact_thickness) {
            contact_corrections_[static_cast<size_t>(vertex_index)] +=
                normal * (config_.contact_thickness - signed_distance);
            ++contact_correction_counts_[static_cast<size_t>(vertex_index)];
            // Track whether any contributing sample was penetrating via the
            // high bit of the count (counts stay small; safe for int32). The
            // corrections themselves are averaged below, so the full-vs-capped
            // decision has to travel separately from the summed vector.
            if (signed_distance < 0.0F) {
                contact_correction_counts_[static_cast<size_t>(vertex_index)] |=
                    static_cast<int32_t>(1u << 30);
            }
        }
    }
    const int32_t vertex_count = static_cast<int32_t>(vertices_.size());
    constexpr int32_t kPenetratingBit = static_cast<int32_t>(1u << 30);
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static)
#endif
    for (int32_t index = 0; index < vertex_count; ++index) {
        int32_t packed = contact_correction_counts_[static_cast<size_t>(index)];
        if (packed == 0) {
            continue;
        }
        const bool penetrating = (packed & kPenetratingBit) != 0;
        const int32_t hits = packed & ~kPenetratingBit;
        if (hits <= 0) {
            continue;
        }
        Vec3 correction =
            contact_corrections_[static_cast<size_t>(index)] / static_cast<float>(hits);
        // Penetrating: project fully to the thickness shell (no soft cap).
        // Outside but inside the shell: allow up to contact_thickness per pass.
        // A smaller cap loses to the seam drag and lets cloth sink into the body.
        if (!penetrating) {
            correction = clamp_length(correction, config_.contact_thickness);
        }
        vertices_[static_cast<size_t>(index)].position += correction;
    }
}

int32_t Solver::gather_body_contacts_auto() {
    // One nearest body face per cloth vertex within the contact search radius.
    // No unlimited (infinite-radius) query: deep interior points that miss the
    // radius are left alone until they re-enter the shell band.
    constexpr float kCollisionSearchM = 0.04F;
    const float search = std::max(config_.contact_thickness, kCollisionSearchM);
    const float pad = search + 1.0e-6F;
    const int32_t vertex_count = static_cast<int32_t>(vertices_.size());
    int32_t candidate_count = 0;
    if (body_bvh_.empty()) {
        std::fill(contact_face_.begin(), contact_face_.end(), -1);
        return 0;
    }
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static) reduction(+ : candidate_count)
#endif
    for (int32_t index = 0; index < vertex_count; ++index) {
        contact_face_[static_cast<size_t>(index)] = -1;
        const Vertex& vertex = vertices_[static_cast<size_t>(index)];
        if (vertex.locked || vertex.inverse_mass <= 0.0F) {
            continue;
        }
        if (!body_bvh_.contains_expanded(vertex.position, pad)) {
            continue;
        }
        int32_t face_index = -1;
        float distance = 0.0F;
        if (!body_bvh_.nearest_face(vertex.position, search, &face_index, &distance)) {
            continue;
        }
        contact_face_[static_cast<size_t>(index)] = face_index;
        ++candidate_count;
    }
    return candidate_count;
}

void Solver::project_body_contacts_auto(bool gather) {
    // Prefer CUDA BVH contact when available (static Body already in VRAM).
    // The OpenMP path remains the fallback when CUDA init failed.
#if defined(YSC_ENABLE_CUDA)
    if (cuda_body_contact_ready()) {
        pack_positions_to_cuda_buffer();
        body_contact_cuda_->upload_cloth(cuda_pos_pack_.data(), cuda_locked_pack_.data());
        project_body_contacts_cuda(nullptr, nullptr, gather, true);
        body_contact_cuda_->download_cloth(cuda_pos_pack_.data());
        unpack_positions_from_cuda_buffer();
        return;
    }
#endif
    // Each unlocked vertex owns at most one face, so the correction write is
    // race-free under OpenMP. Gathering every contact pass is too expensive on
    // high-poly bodies; the advance loop gathers once per substep.
    clear_contact_corrections();
    if (gather) {
        last_auto_candidate_count_ = gather_body_contacts_auto();
    }
    if (last_auto_candidate_count_ <= 0) {
        return;
    }
    const int32_t vertex_count = static_cast<int32_t>(vertices_.size());
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static)
#endif
    for (int32_t index = 0; index < vertex_count; ++index) {
        const int32_t face_index = contact_face_[static_cast<size_t>(index)];
        if (face_index < 0) {
            continue;
        }
        Vertex& vertex = vertices_[static_cast<size_t>(index)];
        if (vertex.locked) {
            continue;
        }
        const Face& face = body_faces_[static_cast<size_t>(face_index)];
        const Vec3& a = body_positions_[static_cast<size_t>(face[0])];
        const Vec3& b = body_positions_[static_cast<size_t>(face[1])];
        const Vec3& c = body_positions_[static_cast<size_t>(face[2])];
        const Vec3 normal = normalized(cross(b - a, c - a));
        const Vec3 closest = closest_triangle_point(vertex.position, a, b, c);
        const float signed_distance = dot(vertex.position - closest, normal);
        if (signed_distance < config_.contact_thickness) {
            Vec3 correction = normal * (config_.contact_thickness - signed_distance);
            // Penetrating (inside the body): full jump to the thickness shell.
            // Outside but still inside the shell: cap at the thickness, which
            // has to stay strong enough to hold against the seam drag.
            if (signed_distance >= 0.0F) {
                correction = clamp_length(correction, config_.contact_thickness);
            }
            vertex.position += correction;
            contact_correction_counts_[static_cast<size_t>(index)] = 1;
        }
    }
}

void Solver::finish_substep(float time_step) {
    const int32_t count = static_cast<int32_t>(vertices_.size());
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static)
#endif
    for (int32_t index = 0; index < count; ++index) {
        Vertex& vertex = vertices_[static_cast<size_t>(index)];
        if (vertex.locked || vertex.inverse_mass <= 0.0F) {
            vertex.velocity = {};
            continue;
        }
        if (seam_driven_[static_cast<size_t>(index)] != 0) {
            // Still being sewn: the span's motion is the drag and the material's
            // answer to it, neither of which the pair may coast on afterwards.
            vertex.velocity = {};
            continue;
        }
        vertex.velocity = (vertex.position - vertex.previous) / time_step;
        if (contact_correction_counts_[static_cast<size_t>(index)] > 0) {
            // Contact is purely dissipative: it may remove kinetic energy but
            // never adds any, so a moving Body cannot fling the cloth.  Gravity
            // re-drives the span each substep, so it still creeps and settles.
            vertex.velocity *= config_.contact_velocity_retention;
        }
    }
}

void Solver::require_finite_state() const {
    for (const Vertex& vertex : vertices_) {
        if (!finite(vertex.position) || !finite(vertex.velocity)) {
            throw std::runtime_error("solver state contains a non-finite vertex");
        }
    }
    for (const Seam& seam : seams_) {
        if (!std::isfinite(seam.target_length) || seam.target_length < 0.0F) {
            throw std::runtime_error("solver state contains an invalid seam target");
        }
    }
}

ysc_stats Solver::advance(const ysc_advance_desc& desc) {
    if (
        !std::isfinite(desc.gravity[0]) || !std::isfinite(desc.gravity[1]) ||
        !std::isfinite(desc.gravity[2])) {
        throw std::invalid_argument("advance descriptor contains an invalid value");
    }
    const bool auto_body = desc.body_candidate_count == YSC_BODY_CANDIDATES_AUTO;
    if (!auto_body && desc.body_candidate_count < 0) {
        throw std::invalid_argument("advance descriptor contains an invalid value");
    }
    if (!auto_body && desc.body_candidate_count > 0 && desc.body_candidates == nullptr) {
        throw std::invalid_argument("advance descriptor has no Body candidates");
    }
    if (!auto_body) {
        for (int32_t index = 0; index < desc.body_candidate_count; ++index) {
            validate_index(desc.body_candidates[index * 2], vertex_count(), "Body candidate vertex");
            validate_index(
                desc.body_candidates[index * 2 + 1],
                static_cast<int32_t>(body_faces_.size()),
                "Body candidate face");
        }
    }

    const int32_t iterations = desc.iterations > 0 ? desc.iterations : config_.iterations;
    const Vec3 gravity{desc.gravity[0], desc.gravity[1], desc.gravity[2]};
    std::vector<Vec3> click_start;
    click_start.reserve(vertices_.size());
    for (const Vertex& vertex : vertices_) {
        click_start.push_back(vertex.position);
    }

    last_auto_candidate_count_ = 0;
    const int32_t external_candidates = auto_body ? 0 : desc.body_candidate_count;
#if defined(YSC_ENABLE_CUDA)
    // With CUDA Body contact, material can stay on device across contact passes
    // (no host BVH). Prefer CUDA materials whenever ready; force env still wins.
    // Without contact CUDA, keep the old 20k-edge gate (H2D/D2H around host BVH).
    const bool contact_cuda = auto_body && cuda_body_contact_ready();
    bool use_cuda_materials = cuda_materials_ready() &&
        (contact_cuda || edges_.size() >= 20000);
#if defined(_MSC_VER)
    char* force = nullptr;
    size_t force_len = 0;
    if (_dupenv_s(&force, &force_len, "YSC_FORCE_MATERIAL") == 0 && force != nullptr) {
        if (std::strcmp(force, "cuda") == 0) {
            use_cuda_materials = cuda_materials_ready();
        } else if (std::strcmp(force, "cpu") == 0) {
            use_cuda_materials = false;
        }
        free(force);
    }
#else
    const char* force = std::getenv("YSC_FORCE_MATERIAL");
    if (force != nullptr) {
        if (std::strcmp(force, "cuda") == 0) {
            use_cuda_materials = cuda_materials_ready();
        } else if (std::strcmp(force, "cpu") == 0) {
            use_cuda_materials = false;
        }
    }
#endif
#else
    const bool use_cuda_materials = false;
    const bool contact_cuda = false;
#endif
    for (int32_t substep = 0; substep < config_.substeps; ++substep) {
        // Ahead of the prediction, so integrate() rebases `previous` onto the
        // dragged position and the pull itself contributes no velocity.  Once
        // per substep, so the iteration count stays a convergence knob and does
        // not change how fast a seam sews shut.
        project_seam_attraction();
        integrate(gravity, config_.time_step);
        update_seam_capture();
#if defined(YSC_ENABLE_CUDA)
        if (use_cuda_materials) {
            // Keep cloth positions on the device for material (+ contact when CUDA).
            pack_positions_to_cuda_buffer();
            pack_seam_captured_to_cuda();
            material_cuda_->upload_positions(cuda_pos_pack_.data());
            material_cuda_->upload_locked(cuda_locked_pack_.data());
            material_cuda_->upload_seam_captured(
                cuda_seam_captured_.data(), static_cast<int32_t>(seams_.size()));
        }
#endif
        for (int32_t iteration = 0; iteration < iterations; ++iteration) {
            const bool reverse = (iteration & 1) != 0;
#if defined(YSC_ENABLE_CUDA)
            if (use_cuda_materials) {
                // Capture is evaluated on host at substep start; mid-substep
                // re-capture would force a download every iteration.
                material_cuda_->project_materials(reverse);
            } else
#endif
            {
                update_seam_capture();
                project_materials(reverse);
            }
            // Contact every other iteration (and always the last). Auto mode
            // gathers nearest body faces once per substep on the first contact
            // pass, then reuses those pairs for later contact passes.
            if ((iteration & 1) == 0 || iteration + 1 == iterations) {
                const bool gather_this_pass = (iteration == 0);
                // finish_substep reads contact hits after the last contact pass
                // of the substep (always the final iteration).
                const bool is_last_contact_of_substep = (iteration + 1 == iterations);
#if defined(YSC_ENABLE_CUDA)
                if (auto_body && contact_cuda && use_cuda_materials) {
                    // Material and contact share device positions (no mid-loop D2H).
                    project_body_contacts_cuda(
                        material_cuda_->device_positions(),
                        material_cuda_->device_locked(),
                        gather_this_pass,
                        is_last_contact_of_substep);
                } else if (auto_body && contact_cuda) {
                    // Material on host: upload, contact on device, download.
                    pack_positions_to_cuda_buffer();
                    body_contact_cuda_->upload_cloth(
                        cuda_pos_pack_.data(), cuda_locked_pack_.data());
                    project_body_contacts_cuda(
                        nullptr, nullptr, gather_this_pass, is_last_contact_of_substep);
                    body_contact_cuda_->download_cloth(cuda_pos_pack_.data());
                    unpack_positions_from_cuda_buffer();
                } else {
                    // Host contact path. If materials just ran on device, sync down.
                    if (use_cuda_materials) {
                        material_cuda_->download_positions(cuda_pos_pack_.data());
                        unpack_positions_from_cuda_buffer();
                    }
                    if (auto_body) {
                        project_body_contacts_auto(gather_this_pass);
                    } else {
                        project_body_contacts_external(
                            desc.body_candidates, desc.body_candidate_count);
                    }
                    if (use_cuda_materials) {
                        pack_positions_to_cuda_buffer();
                        material_cuda_->upload_positions(cuda_pos_pack_.data());
                    }
                }
#else
                if (auto_body) {
                    project_body_contacts_auto(gather_this_pass);
                } else {
                    project_body_contacts_external(
                        desc.body_candidates, desc.body_candidate_count);
                }
#endif
            }
        }
#if defined(YSC_ENABLE_CUDA)
        if (use_cuda_materials) {
            material_cuda_->download_positions(cuda_pos_pack_.data());
            unpack_positions_from_cuda_buffer();
        }
#endif
        finish_substep(config_.time_step);
    }
    require_finite_state();

    ysc_stats stats{};
    stats.substeps = config_.substeps;
    stats.iterations = iterations;
    stats.seam_count = seam_count();
    stats.captured_seam_count = static_cast<int32_t>(std::count_if(
        seams_.begin(), seams_.end(), [](const Seam& seam) { return seam.captured; }));
    stats.edge_count = static_cast<int32_t>(edges_.size());
    stats.quad_count = static_cast<int32_t>(quads_.size());
    stats.bend_count = static_cast<int32_t>(bends_.size());
    stats.body_candidate_count = auto_body ? last_auto_candidate_count_ : external_candidates;
    for (size_t index = 0; index < vertices_.size(); ++index) {
        stats.maximum_displacement = std::max(
            stats.maximum_displacement,
            length(vertices_[index].position - click_start[index]));
    }
    return stats;
}

}  // namespace ysc
