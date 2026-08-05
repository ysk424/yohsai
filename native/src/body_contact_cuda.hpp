// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "body_bvh.hpp"

#include <cstdint>

namespace ysc {

// CUDA Body contact: static Body BVH lives in VRAM (body is fixed at create).
// Cloth positions are either uploaded each pass into owned buffers, or the
// caller passes external device pointers (e.g. MaterialCuda positions) so
// contact can run without host BVH work or mid-substep D2H/H2D for materials.
class BodyContactCuda {
public:
    BodyContactCuda() = default;
    BodyContactCuda(const BodyContactCuda&) = delete;
    BodyContactCuda& operator=(const BodyContactCuda&) = delete;
    ~BodyContactCuda();

    [[nodiscard]] static bool device_available() noexcept;

    // Upload Body BVH once. Returns false on failure (caller keeps OpenMP path).
    [[nodiscard]] bool init(int32_t cloth_vertex_count, const BodyBvh& bvh);

    void destroy() noexcept;
    [[nodiscard]] bool ready() const noexcept { return ready_; }

    // Owned cloth buffers (material on host).
    void upload_cloth(const float* positions_xyz, const int32_t* locked);
    void download_cloth(float* positions_xyz) const;
    // 0/1 hit flags for finish_substep velocity dissipation.
    void download_hits(int32_t* hits) const;

    [[nodiscard]] float* device_positions() noexcept { return d_pos_; }
    [[nodiscard]] int32_t* device_locked() noexcept { return d_locked_; }

    // When external_positions is non-null it must be device memory with
    // 3*vertex_count floats (and external_locked device int32 per vertex).
    // When null, owned d_pos_/d_locked_ are used (upload_cloth first).
    // Returns candidate count after a gather pass (0 if gather==false).
    int32_t project(
        float* external_positions,
        int32_t* external_locked,
        float search_radius,
        float contact_thickness,
        bool gather);

    [[nodiscard]] int32_t last_candidate_count() const noexcept { return last_candidate_count_; }

private:
    bool ready_ = false;
    int32_t vertex_count_ = 0;
    int32_t node_count_ = 0;
    int32_t leaf_count_ = 0;
    int32_t face_count_ = 0;
    int32_t last_candidate_count_ = 0;

    float bounds_min_[3]{};
    float bounds_max_[3]{};

    BodyBvh::GpuNode* d_nodes_ = nullptr;
    int32_t* d_leaves_ = nullptr;
    BodyBvh::GpuFace* d_bvh_faces_ = nullptr;  // leaf-ref order for BVH walk
    // Dense by original face_index for project (and deep-penetration test).
    float* d_face_tri_ = nullptr;  // 9 floats per face: a,b,c

    float* d_pos_ = nullptr;
    int32_t* d_locked_ = nullptr;
    int32_t* d_contact_face_ = nullptr;
    int32_t* d_hit_ = nullptr;
    int32_t* d_candidate_count_ = nullptr;
};

}  // namespace ysc
