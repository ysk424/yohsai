// SPDX-License-Identifier: GPL-3.0-or-later
#include "yohsai_cosserat/c_api.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

float distance(const float* left, const float* right) {
    const float x = right[0] - left[0];
    const float y = right[1] - left[1];
    const float z = right[2] - left[2];
    return std::sqrt(x * x + y * y + z * z);
}

struct NativeSolver {
    ysc_handle handle = nullptr;
    int32_t vertex_count = 0;
    int32_t seam_count = 0;

    NativeSolver(const ysc_create_desc& desc, const ysc_config& config) {
        std::array<char, 512> error{};
        const ysc_status status = ysc_create(
            &desc, &config, &handle, error.data(), static_cast<int32_t>(error.size()));
        require(status == YSC_STATUS_OK, std::string("ysc_create failed: ") + error.data());
        const ysc_status count_status = ysc_get_counts(
            handle,
            &vertex_count,
            &seam_count,
            error.data(),
            static_cast<int32_t>(error.size()));
        require(count_status == YSC_STATUS_OK, std::string("ysc_get_counts failed: ") + error.data());
    }

    NativeSolver(const NativeSolver&) = delete;
    NativeSolver& operator=(const NativeSolver&) = delete;

    ~NativeSolver() {
        ysc_destroy(handle);
    }

    ysc_stats advance(
        const std::array<float, 3>& gravity,
        int32_t iterations = 0,
        const std::vector<int32_t>& body_candidates = {}) {
        ysc_advance_desc desc{};
        std::copy(gravity.begin(), gravity.end(), desc.gravity);
        desc.iterations = iterations;
        desc.body_candidate_count = static_cast<int32_t>(body_candidates.size() / 2);
        desc.body_candidates = body_candidates.empty() ? nullptr : body_candidates.data();
        ysc_stats stats{};
        std::array<char, 512> error{};
        const ysc_status status = ysc_advance(
            handle, &desc, &stats, error.data(), static_cast<int32_t>(error.size()));
        require(status == YSC_STATUS_OK, std::string("ysc_advance failed: ") + error.data());
        return stats;
    }

    std::pair<std::vector<float>, std::vector<float>> state() const {
        std::vector<float> positions(static_cast<size_t>(vertex_count) * 3);
        std::vector<float> velocities(static_cast<size_t>(vertex_count) * 3);
        std::array<char, 512> error{};
        const ysc_status status = ysc_copy_state(
            handle,
            positions.data(),
            velocities.data(),
            error.data(),
            static_cast<int32_t>(error.size()));
        require(status == YSC_STATUS_OK, std::string("ysc_copy_state failed: ") + error.data());
        return {positions, velocities};
    }

    void replace_state(
        const std::vector<float>& positions,
        const std::vector<float>& velocities,
        const std::vector<int32_t>& locked) {
        std::array<char, 512> error{};
        const ysc_status status = ysc_replace_state(
            handle,
            positions.data(),
            velocities.data(),
            locked.data(),
            error.data(),
            static_cast<int32_t>(error.size()));
        require(status == YSC_STATUS_OK, std::string("ysc_replace_state failed: ") + error.data());
    }

    std::vector<float> seam_state() const {
        std::vector<float> result(static_cast<size_t>(seam_count));
        std::array<char, 512> error{};
        const ysc_status status = ysc_copy_seam_state(
            handle, result.data(), error.data(), static_cast<int32_t>(error.size()));
        require(status == YSC_STATUS_OK, std::string("ysc_copy_seam_state failed: ") + error.data());
        return result;
    }
};

ysc_config test_config(int32_t substeps = 1) {
    ysc_config config{};
    require(ysc_default_config(&config) == YSC_STATUS_OK, "default config failed");
    config.substeps = substeps;
    config.iterations = 8;
    config.maximum_position_correction = 0.05F;
    return config;
}

ysc_create_desc particle_desc(
    const std::vector<float>& positions,
    const std::vector<int32_t>& locked,
    const std::vector<int32_t>& seams = {},
    const std::vector<int32_t>& edges = {},
    const std::vector<float>& edge_rest = {},
    const std::vector<int32_t>& quads = {},
    const std::vector<float>& quad_rest = {},
    const std::vector<int32_t>& bends = {},
    const std::vector<float>& bend_rest = {}) {
    ysc_create_desc desc{};
    desc.vertex_count = static_cast<int32_t>(positions.size() / 3);
    desc.positions = positions.data();
    desc.locked = locked.data();
    desc.seam_count = static_cast<int32_t>(seams.size() / 2);
    desc.seams = seams.empty() ? nullptr : seams.data();
    desc.edge_count = static_cast<int32_t>(edges.size() / 2);
    desc.edges = edges.empty() ? nullptr : edges.data();
    desc.edge_rest_lengths = edge_rest.empty() ? nullptr : edge_rest.data();
    desc.quad_count = static_cast<int32_t>(quads.size() / 4);
    desc.quads = quads.empty() ? nullptr : quads.data();
    desc.quad_rest_metrics = quad_rest.empty() ? nullptr : quad_rest.data();
    desc.bend_count = static_cast<int32_t>(bends.size() / 3);
    desc.bends = bends.empty() ? nullptr : bends.data();
    desc.bend_rest_lengths = bend_rest.empty() ? nullptr : bend_rest.data();
    return desc;
}

void test_api_and_invalid_input() {
    require(ysc_get_api_version() == YSC_API_VERSION, "API version mismatch");
    require(ysc_default_config(nullptr) == YSC_STATUS_INVALID_ARGUMENT, "null config was accepted");

    const std::vector<float> positions{0.0F, 0.0F, 0.0F, 1.0F, 0.0F, 0.0F};
    const std::vector<int32_t> locked{0, 0};
    ysc_create_desc desc = particle_desc(positions, locked);
    desc.positions = nullptr;
    ysc_handle handle = nullptr;
    std::array<char, 512> error{};
    const ysc_config config = test_config();
    const ysc_status status = ysc_create(
        &desc, &config, &handle, error.data(), static_cast<int32_t>(error.size()));
    require(status == YSC_STATUS_INVALID_ARGUMENT, "missing positions were accepted");
    require(handle == nullptr, "failed create returned a handle");
}

void test_no_hidden_force() {
    const std::vector<float> positions{
        0.0F, 0.0F, 0.0F,
        1.3F, 0.2F, 0.1F,
        1.1F, 0.4F, 1.4F,
        -0.2F, -0.1F, 0.9F,
    };
    const std::vector<int32_t> locked(4, 0);
    NativeSolver solver(particle_desc(positions, locked), test_config());
    const ysc_stats stats = solver.advance({0.0F, 0.0F, 0.0F}, 64);
    const auto [solved, velocities] = solver.state();
    require(solved == positions, "positions changed without gravity, seam, or Body contact");
    require(
        std::all_of(velocities.begin(), velocities.end(), [](float value) { return value == 0.0F; }),
        "velocity appeared without an allowed force");
    require(stats.seam_count == 0 && stats.body_candidate_count == 0, "unexpected active input");
}

void test_gravity_is_the_only_free_particle_force() {
    const std::vector<float> positions{
        0.0F, 0.0F, 1.0F,
        1.0F, 0.0F, 1.0F,
    };
    const std::vector<int32_t> locked{0, 1};
    NativeSolver solver(particle_desc(positions, locked), test_config(4));
    solver.advance({0.0F, 0.0F, -1.0F});
    const auto [solved, velocities] = solver.state();
    const float time_step = 1.0F / 240.0F;
    const float expected_drop = 10.0F * time_step * time_step;
    require(std::abs(solved[2] - (1.0F - expected_drop)) < 1.0e-6F, "gravity motion is wrong");
    require(std::abs(velocities[2] + 4.0F * time_step) < 1.0e-4F, "gravity velocity is wrong");
    require(distance(solved.data() + 3, positions.data() + 3) == 0.0F, "locked vertex moved");
}

void test_seam_target_is_fixed_at_zero() {
    const std::vector<float> positions{
        0.0F, 0.0F, 0.0F,
        2.0F, 0.0F, 0.0F,
    };
    const std::vector<int32_t> locked{0, 0};
    const std::vector<int32_t> seams{0, 1};
    NativeSolver solver(particle_desc(positions, locked, seams), test_config());

    solver.advance({0.0F, 0.0F, 0.0F});
    const auto [pulled, _pulled_velocities] = solver.state();
    require(distance(pulled.data(), pulled.data() + 3) < 2.0F, "extended seam did not pull");
    const std::vector<float> target = solver.seam_state();
    require(target.size() == 1 && target[0] == 0.0F, "seam target is not fixed at zero");
}

void test_seam_attraction_is_distance_independent_and_captures() {
    const std::vector<int32_t> unlocked{0, 0};
    const std::vector<int32_t> seams{0, 1};
    const ysc_config config = test_config();

    const std::vector<float> far_positions{
        0.0F, 0.0F, 0.0F,
        0.5F, 0.0F, 0.0F,
    };
    NativeSolver far_solver(particle_desc(far_positions, unlocked, seams), config);
    far_solver.advance({0.0F, 0.0F, 0.0F}, 1);
    const auto [far_solved, _far_velocities] = far_solver.state();
    const float far_reduction = 0.5F - distance(far_solved.data(), far_solved.data() + 3);

    const std::vector<float> near_positions{
        0.0F, 0.0F, 0.0F,
        0.05F, 0.0F, 0.0F,
    };
    NativeSolver near_solver(particle_desc(near_positions, unlocked, seams), config);
    near_solver.advance({0.0F, 0.0F, 0.0F}, 1);
    const auto [near_solved, _near_velocities] = near_solver.state();
    const float near_reduction = 0.05F - distance(near_solved.data(), near_solved.data() + 3);
    require(far_reduction > 0.0F && near_reduction > 0.0F, "constant seam attraction did not pull");
    require(
        std::abs(far_reduction - near_reduction) < 1.0e-6F,
        "seam attraction still depends on pair distance");

    const std::vector<float> capture_positions{
        0.0F, 0.0F, 0.0F,
        0.001F, 0.0F, 0.0F,
    };
    NativeSolver capture_solver(particle_desc(capture_positions, unlocked, seams), config);
    const ysc_stats stats = capture_solver.advance({0.0F, 0.0F, 0.0F}, 1);
    const auto [captured, _captured_velocities] = capture_solver.state();
    require(distance(captured.data(), captured.data() + 3) < 1.0e-7F, "near seam was not captured");
    require(stats.captured_seam_count == 1, "captured seam was not reported");
}

void test_square_metric_is_rest_invariant_and_transmits_seam_motion() {
    const std::vector<float> positions{
        0.0F, 0.0F, 0.0F,
        1.0F, 0.0F, 0.0F,
        1.0F, 1.0F, 0.0F,
        0.0F, 1.0F, 0.0F,
        2.0F, 0.0F, 0.0F,
    };
    const std::vector<int32_t> locked(5, 0);
    const std::vector<int32_t> seams{1, 4};
    const std::vector<int32_t> edges{0, 1, 1, 2, 2, 3, 3, 0};
    const std::vector<float> edge_rest(4, 1.0F);
    const std::vector<int32_t> quads{0, 1, 2, 3};
    const std::vector<float> quad_rest{1.0F, 1.0F, 0.0F};
    ysc_config config = test_config();
    config.iterations = 32;
    NativeSolver solver(
        particle_desc(positions, locked, seams, edges, edge_rest, quads, quad_rest), config);

    solver.advance({0.0F, 0.0F, 0.0F}, 32);
    const auto [solved, _velocities] = solver.state();
    require(solved[0] > positions[0], "material edges did not transmit the seam motion into the quad");
    require(distance(solved.data(), solved.data() + 3) < 1.02F, "quad edge stretched excessively");
    require(distance(solved.data(), solved.data() + 3) > 0.98F, "quad edge compressed excessively");
}

void test_material_rest_is_rigid_transform_invariant() {
    // One square and one straight material-axis triple after the same rigid
    // 3D rotation/translation.  Neither term may encode a world or Body axis.
    const std::vector<float> positions{
        3.0F, -2.0F, 1.0F,
        3.0F, -1.0F, 1.0F,
        2.2928932F, -1.0F, 1.7071068F,
        2.2928932F, -2.0F, 1.7071068F,
        3.7071068F, -2.0F, 0.2928932F,
        3.0F, -2.0F, 1.0F,
        2.2928932F, -2.0F, 1.7071068F,
    };
    const std::vector<int32_t> unlocked(7, 0);
    const std::vector<int32_t> edges{0, 1, 1, 2, 2, 3, 3, 0};
    const std::vector<float> edge_rest(4, 1.0F);
    const std::vector<int32_t> quads{0, 1, 2, 3};
    const std::vector<float> quad_rest{1.0F, 1.0F, 0.0F};
    const std::vector<int32_t> bends{4, 5, 6};
    const std::vector<float> bend_rest{1.0F, 1.0F};
    NativeSolver solver(
        particle_desc(
            positions, unlocked, {}, edges, edge_rest, quads, quad_rest, bends, bend_rest),
        test_config());
    solver.advance({0.0F, 0.0F, 0.0F}, 16);
    const auto [solved, velocities] = solver.state();
    float maximum_change = 0.0F;
    for (size_t index = 0; index < positions.size(); index += 3) {
        maximum_change = std::max(maximum_change, distance(positions.data() + index, solved.data() + index));
    }
    require(maximum_change < 1.0e-5F, "material rest state changed under a rigid transform");
    require(
        std::all_of(velocities.begin(), velocities.end(), [](float value) { return std::abs(value) < 1.0e-3F; }),
        "material rest state generated velocity under a rigid transform");
}

void test_quad_shear_and_axial_bend_reduce_their_energy() {
    const std::vector<float> shear_positions{
        0.0F, 0.0F, 0.0F,
        1.0F, 0.0F, 0.0F,
        1.5F, 1.0F, 0.0F,
        0.5F, 1.0F, 0.0F,
    };
    const std::vector<int32_t> unlocked(4, 0);
    const std::vector<int32_t> quads{0, 1, 2, 3};
    const std::vector<float> quad_rest{1.0F, 1.0F, 0.0F};
    ysc_config shear_config = test_config();
    shear_config.shear_relaxation = 0.5F;
    NativeSolver shear_solver(
        particle_desc(shear_positions, unlocked, {}, {}, {}, quads, quad_rest), shear_config);
    shear_solver.advance({0.0F, 0.0F, 0.0F}, 8);
    const auto [unsheared, _shear_velocity] = shear_solver.state();
    const float top_offset_before = shear_positions[9] - shear_positions[0];
    const float top_offset_after = unsheared[9] - unsheared[0];
    require(std::abs(top_offset_after) < std::abs(top_offset_before), "quad shear energy did not decrease");

    const std::vector<float> bend_positions{
        -1.0F, 0.0F, 0.0F,
        0.0F, 0.0F, 0.5F,
        1.0F, 0.0F, 0.0F,
    };
    const std::vector<int32_t> bend_unlocked(3, 0);
    const std::vector<int32_t> bends{0, 1, 2};
    const std::vector<float> bend_rest{1.0F, 1.0F};
    ysc_config bend_config = test_config();
    bend_config.bend_relaxation = 0.25F;
    NativeSolver bend_solver(
        particle_desc(bend_positions, bend_unlocked, {}, {}, {}, {}, {}, bends, bend_rest), bend_config);
    bend_solver.advance({0.0F, 0.0F, 0.0F}, 1);
    const auto [unbent, _bend_velocity] = bend_solver.state();
    require(unbent[5] < bend_positions[5], "axial bend energy did not decrease");
}

void test_body_correction_requires_a_contact_candidate() {
    const std::vector<float> positions{
        0.1F, 0.1F, -0.05F,
        0.2F, 0.1F, -0.05F,
    };
    const std::vector<int32_t> locked{0, 0};
    const std::vector<float> body_positions{
        0.0F, 0.0F, 0.0F,
        1.0F, 0.0F, 0.0F,
        0.0F, 1.0F, 0.0F,
    };
    const std::vector<int32_t> body_faces{0, 1, 2};
    ysc_create_desc desc = particle_desc(positions, locked);
    desc.body_vertex_count = 3;
    desc.body_positions = body_positions.data();
    desc.body_face_count = 1;
    desc.body_faces = body_faces.data();
    NativeSolver solver(desc, test_config());
    solver.advance({0.0F, 0.0F, 0.0F}, 1, {0, 0});
    const auto [solved, _velocities] = solver.state();
    require(solved[2] > positions[2], "Body candidate did not apply contact correction");
    require(solved[5] == positions[5], "Body moved a vertex with no contact candidate");
}

}  // namespace

int main() {
    try {
        test_api_and_invalid_input();
        test_no_hidden_force();
        test_gravity_is_the_only_free_particle_force();
        test_seam_target_is_fixed_at_zero();
        test_seam_attraction_is_distance_independent_and_captures();
        test_square_metric_is_rest_invariant_and_transmits_seam_motion();
        test_material_rest_is_rigid_transform_invariant();
        test_quad_shear_and_axial_bend_reduce_their_energy();
        test_body_correction_requires_a_contact_candidate();
        std::cout << "All square-lattice cloth native tests passed.\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& exception) {
        std::cerr << "Test failure: " << exception.what() << '\n';
        return EXIT_FAILURE;
    }
}
