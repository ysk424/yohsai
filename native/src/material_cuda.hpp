// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <cstdint>
#include <vector>

namespace ysc {

// Optional CUDA backend for coloured material projections (seams / edges /
// quads / bends). Colours stay sequential (Gauss-Seidel across colours);
// constraints within a colour run as one kernel. CPU OpenMP remains the
// fallback when CUDA is unavailable.
class MaterialCuda {
public:
    struct Edge {
        int32_t a = 0;
        int32_t b = 0;
        float rest_length = 0.0F;
    };
    struct Quad {
        int32_t v0 = 0;
        int32_t v1 = 0;
        int32_t v2 = 0;
        int32_t v3 = 0;
        float rest_u_squared = 0.0F;
        float rest_v_squared = 0.0F;
        float rest_shear = 0.0F;
    };
    struct Bend {
        int32_t a = 0;
        int32_t b = 0;
        int32_t c = 0;
        float previous_rest = 0.0F;
        float next_rest = 0.0F;
    };
    struct Seam {
        int32_t a = 0;
        int32_t b = 0;
        float target_length = 0.0F;
    };
    struct Params {
        float stretch_relaxation = 1.0F;
        float shear_relaxation = 0.02F;
        float bend_relaxation = 0.02F;
        float stretch_limit = 0.05F;
        float maximum_position_correction = 0.005F;
    };

    MaterialCuda() = default;
    MaterialCuda(const MaterialCuda&) = delete;
    MaterialCuda& operator=(const MaterialCuda&) = delete;
    ~MaterialCuda();

    [[nodiscard]] static bool device_available() noexcept;

    // Returns false if CUDA init/upload failed (caller keeps CPU path).
    [[nodiscard]] bool init(
        int32_t vertex_count,
        const float* inverse_masses,
        const int32_t* locked,
        const std::vector<Edge>& edges,
        const std::vector<int32_t>& edge_colour_offsets,
        const std::vector<int32_t>& edge_colour_indices,
        const std::vector<Quad>& quads,
        const std::vector<int32_t>& quad_colour_offsets,
        const std::vector<int32_t>& quad_colour_indices,
        const std::vector<Bend>& bends,
        const std::vector<int32_t>& bend_colour_offsets,
        const std::vector<int32_t>& bend_colour_indices,
        const std::vector<Seam>& seams,
        const std::vector<int32_t>& seam_colour_offsets,
        const std::vector<int32_t>& seam_colour_indices);

    void destroy() noexcept;
    [[nodiscard]] bool ready() const noexcept { return ready_; }

    // Pack host positions (xyz per vertex) to device.
    void upload_positions(const float* positions_xyz);
    void download_positions(float* positions_xyz) const;
    void upload_locked(const int32_t* locked);

    // 0/1 per seam; only captured seams project.
    void upload_seam_captured(const uint8_t* captured, int32_t seam_count);

    void set_params(const Params& params) { params_ = params; }

    // One full material pass used inside each solver iteration.
    // Device buffers must already hold positions (and seam flags).
    void project_materials(bool reverse);

private:
    bool ready_ = false;
    int32_t vertex_count_ = 0;
    int32_t edge_count_ = 0;
    int32_t quad_count_ = 0;
    int32_t bend_count_ = 0;
    int32_t seam_count_ = 0;
    Params params_{};

    float* d_pos_ = nullptr;           // float3 as float[3*n]
    float* d_inv_mass_ = nullptr;
    int32_t* d_locked_ = nullptr;

    int32_t* d_edge_ab_ = nullptr;     // 2 * edge_count
    float* d_edge_rest_ = nullptr;
    int32_t* d_edge_colour_offsets_ = nullptr;
    int32_t* d_edge_colour_indices_ = nullptr;
    int32_t edge_colour_count_ = 0;

    int32_t* d_quad_v_ = nullptr;      // 4 * quad_count
    float* d_quad_metric_ = nullptr;   // 3 * quad_count
    int32_t* d_quad_colour_offsets_ = nullptr;
    int32_t* d_quad_colour_indices_ = nullptr;
    int32_t quad_colour_count_ = 0;

    int32_t* d_bend_v_ = nullptr;      // 3 * bend_count
    float* d_bend_rest_ = nullptr;     // 2 * bend_count
    int32_t* d_bend_colour_offsets_ = nullptr;
    int32_t* d_bend_colour_indices_ = nullptr;
    int32_t bend_colour_count_ = 0;

    int32_t* d_seam_ab_ = nullptr;
    float* d_seam_target_ = nullptr;
    uint8_t* d_seam_captured_ = nullptr;
    int32_t* d_seam_colour_offsets_ = nullptr;
    int32_t* d_seam_colour_indices_ = nullptr;
    int32_t seam_colour_count_ = 0;

    std::vector<int32_t> edge_colour_offsets_host_;
    std::vector<int32_t> quad_colour_offsets_host_;
    std::vector<int32_t> bend_colour_offsets_host_;
    std::vector<int32_t> seam_colour_offsets_host_;

};

}  // namespace ysc
