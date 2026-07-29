// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "body_bvh.hpp"
#include "math.hpp"
#include "yohsai_solver/c_api.h"

#include <array>
#include <cstdint>
#include <memory>
#include <vector>

#if defined(YSC_ENABLE_CUDA)
#  include "material_cuda.hpp"
#endif

namespace ysc {

// Independent-set colors of a constraint family. Constraints in one color never
// share a vertex, so a color may be projected with OpenMP without write races.
// Colors themselves remain sequential (Gauss-Seidel across colors, Jacobi-like
// within a color).
using ColorGroups = std::vector<std::vector<int32_t>>;

class Solver {
public:
    Solver(const ysc_create_desc& desc, const ysc_config& config);

    [[nodiscard]] int32_t vertex_count() const noexcept;
    [[nodiscard]] int32_t seam_count() const noexcept;

    void replace_state(
        const float* positions,
        const float* velocities,
        const int32_t* locked);
    void copy_state(float* positions, float* velocities) const;

    void replace_seam_state(const float* target_lengths);
    void copy_seam_state(float* target_lengths) const;

    ysc_stats advance(const ysc_advance_desc& desc);

private:
    struct Vertex {
        Vec3 position;
        Vec3 previous;
        Vec3 velocity;
        float inverse_mass = 1.0F;
        bool locked = false;
    };

    struct Seam {
        int32_t a = 0;
        int32_t b = 0;
        float target_length = 0.0F;
        bool captured = false;
    };

    struct Edge {
        int32_t a = 0;
        int32_t b = 0;
        float rest_length = 0.0F;
    };

    struct Quad {
        std::array<int32_t, 4> vertices{};
        float rest_u_squared = 0.0F;
        float rest_v_squared = 0.0F;
        float rest_shear = 0.0F;
    };

    struct Bend {
        std::array<int32_t, 3> vertices{};
        float previous_rest_length = 0.0F;
        float next_rest_length = 0.0F;
    };

    using Face = std::array<int32_t, 3>;

    ysc_config config_{};
    std::vector<Vertex> vertices_;
    std::vector<Seam> seams_;
    std::vector<Edge> edges_;
    std::vector<Quad> quads_;
    std::vector<Bend> bends_;
    std::vector<Vec3> body_positions_;
    std::vector<Face> body_faces_;
    BodyBvh body_bvh_;
    // Per-vertex nearest body face for the current contact pass (-1 = none).
    std::vector<int32_t> contact_face_;
    int32_t last_auto_candidate_count_ = 0;
    std::vector<Vec3> contact_corrections_;
    std::vector<int32_t> contact_correction_counts_;
    std::vector<int32_t> seam_driven_;

    ColorGroups seam_colors_;
    ColorGroups edge_colors_;
    ColorGroups quad_colors_;
    ColorGroups bend_colors_;
    // Flattened colour tables for tighter OpenMP loops.
    std::vector<int32_t> edge_colour_offsets_;
    std::vector<int32_t> edge_colour_indices_;
    std::vector<int32_t> quad_colour_offsets_;
    std::vector<int32_t> quad_colour_indices_;
    std::vector<int32_t> bend_colour_offsets_;
    std::vector<int32_t> bend_colour_indices_;
    std::vector<int32_t> seam_colour_offsets_;
    std::vector<int32_t> seam_colour_indices_;

#if defined(YSC_ENABLE_CUDA)
    std::unique_ptr<MaterialCuda> material_cuda_;
    std::vector<float> cuda_pos_pack_;
    std::vector<uint8_t> cuda_seam_captured_;
    std::vector<int32_t> cuda_locked_pack_;
    std::vector<float> cuda_inv_mass_pack_;
#endif

    void validate_config() const;
    void build_color_groups();
    static void flatten_colors(
        const ColorGroups& groups,
        std::vector<int32_t>& offsets,
        std::vector<int32_t>& indices);
    void project_seam_attraction();
    void integrate(const Vec3& gravity, float time_step);
    void update_seam_capture();
    void project_seams();
    void project_edge(const Edge& edge);
    void project_edges(bool reverse);
    void project_quad(const Quad& quad);
    void project_quad_shear(bool reverse);
    void project_bend(const Bend& bend);
    void project_bends(bool reverse);
    void project_materials(bool reverse);
    void project_distance(int32_t a, int32_t b, float target_length, float relaxation);
#if defined(YSC_ENABLE_CUDA)
    void init_material_cuda();
    void pack_positions_to_cuda_buffer();
    void unpack_positions_from_cuda_buffer();
    void pack_seam_captured_to_cuda();
    [[nodiscard]] bool cuda_materials_ready() const noexcept;
#endif
    void project_body_contacts_external(const int32_t* candidates, int32_t count);
    // gather=true rebuilds nearest body faces; false reuses contact_face_.
    void project_body_contacts_auto(bool gather);
    void finish_substep(float time_step);
    [[nodiscard]] Vec3 closest_triangle_point(
        const Vec3& point,
        const Vec3& a,
        const Vec3& b,
        const Vec3& c) const;
    void clear_contact_corrections();
    void require_finite_state() const;
    [[nodiscard]] int32_t gather_body_contacts_auto();
};

ysc_config default_config();

}  // namespace ysc
