// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "math.hpp"
#include "yohsai_cosserat/c_api.h"

#include <array>
#include <cstdint>
#include <vector>

namespace ysc {

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
    std::vector<Vec3> contact_corrections_;
    std::vector<int32_t> contact_correction_counts_;

    void validate_config() const;
    void apply_seam_attraction(float time_step);
    void integrate(const Vec3& gravity, float time_step);
    void update_seam_capture();
    void project_seams();
    void project_edges(bool reverse);
    void project_quad_shear(bool reverse);
    void project_bends(bool reverse);
    void project_distance(int32_t a, int32_t b, float target_length, float relaxation);
    void project_body_contacts(const int32_t* candidates, int32_t count);
    void finish_substep(float time_step);
    [[nodiscard]] Vec3 closest_triangle_point(
        const Vec3& point,
        const Vec3& a,
        const Vec3& b,
        const Vec3& c) const;
    void clear_contact_corrections();
    void require_finite_state() const;
};

ysc_config default_config();

}  // namespace ysc
