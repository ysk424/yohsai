// SPDX-License-Identifier: GPL-3.0-or-later
#include "body_contact_cuda.hpp"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace ysc {
namespace {

constexpr float kEpsilon = 1.0e-8F;
constexpr int kThreads = 256;

#define YSC_CUDA_CHECK(call)                                                   \
    do {                                                                       \
        const cudaError_t ysc_err = (call);                                    \
        if (ysc_err != cudaSuccess) {                                          \
            throw std::runtime_error(                                          \
                std::string("CUDA error: ") + cudaGetErrorString(ysc_err));    \
        }                                                                      \
    } while (0)

__device__ inline float3 make_f3(float x, float y, float z) {
    return make_float3(x, y, z);
}
__device__ inline float3 operator+(float3 a, float3 b) {
    return make_f3(a.x + b.x, a.y + b.y, a.z + b.z);
}
__device__ inline float3 operator-(float3 a, float3 b) {
    return make_f3(a.x - b.x, a.y - b.y, a.z - b.z);
}
__device__ inline float3 operator*(float3 a, float s) {
    return make_f3(a.x * s, a.y * s, a.z * s);
}
__device__ inline float3 operator*(float s, float3 a) { return a * s; }
__device__ inline float3& operator+=(float3& a, float3 b) {
    a.x += b.x;
    a.y += b.y;
    a.z += b.z;
    return a;
}
__device__ inline float dot3(float3 a, float3 b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
}
__device__ inline float3 cross3(float3 a, float3 b) {
    return make_f3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
}
__device__ inline float length3(float3 a) { return sqrtf(dot3(a, a)); }
__device__ inline float3 normalized3(float3 a) {
    const float len = length3(a);
    if (!(len > kEpsilon)) {
        return make_f3(0.0F, 0.0F, 1.0F);
    }
    return a * (1.0F / len);
}
__device__ inline float3 clamp_length3(float3 v, float max_len) {
    const float len = length3(v);
    if (!(len > max_len) || !(max_len > 0.0F)) {
        return v;
    }
    return v * (max_len / len);
}

__device__ inline float3 load_f3(const float* p, int index) {
    return make_f3(p[index * 3], p[index * 3 + 1], p[index * 3 + 2]);
}
__device__ inline void store_f3(float* p, int index, float3 v) {
    p[index * 3] = v.x;
    p[index * 3 + 1] = v.y;
    p[index * 3 + 2] = v.z;
}
__device__ inline float3 load3(const float* v) {
    return make_f3(v[0], v[1], v[2]);
}

__device__ float aabb_distance(float3 point, const float bmin[3], const float bmax[3]) {
    const float dx = point.x < bmin[0] ? bmin[0] - point.x : (point.x > bmax[0] ? point.x - bmax[0] : 0.0F);
    const float dy = point.y < bmin[1] ? bmin[1] - point.y : (point.y > bmax[1] ? point.y - bmax[1] : 0.0F);
    const float dz = point.z < bmin[2] ? bmin[2] - point.z : (point.z > bmax[2] ? point.z - bmax[2] : 0.0F);
    return sqrtf(dx * dx + dy * dy + dz * dz);
}

__device__ bool contains_expanded(
    float3 point,
    const float bmin[3],
    const float bmax[3],
    float padding) {
    return point.x >= bmin[0] - padding && point.x <= bmax[0] + padding &&
        point.y >= bmin[1] - padding && point.y <= bmax[1] + padding &&
        point.z >= bmin[2] - padding && point.z <= bmax[2] + padding;
}

__device__ float3 closest_triangle_point(float3 point, float3 a, float3 b, float3 c) {
    const float3 ab = b - a;
    const float3 ac = c - a;
    const float3 ap = point - a;
    const float d1 = dot3(ab, ap);
    const float d2 = dot3(ac, ap);
    if (d1 <= 0.0F && d2 <= 0.0F) {
        return a;
    }
    const float3 bp = point - b;
    const float d3 = dot3(ab, bp);
    const float d4 = dot3(ac, bp);
    if (d3 >= 0.0F && d4 <= d3) {
        return b;
    }
    const float vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0F && d1 >= 0.0F && d3 <= 0.0F) {
        return a + ab * (d1 / (d1 - d3));
    }
    const float3 cp = point - c;
    const float d5 = dot3(ab, cp);
    const float d6 = dot3(ac, cp);
    if (d6 >= 0.0F && d5 <= d6) {
        return c;
    }
    const float vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0F && d2 >= 0.0F && d6 <= 0.0F) {
        return a + ac * (d2 / (d2 - d6));
    }
    const float va = d3 * d6 - d5 * d4;
    if (va <= 0.0F && (d4 - d3) >= 0.0F && (d5 - d6) >= 0.0F) {
        return b + (c - b) * ((d4 - d3) / ((d4 - d3) + (d5 - d6)));
    }
    const float inverse = 1.0F / (va + vb + vc);
    return a + ab * (vb * inverse) + ac * (vc * inverse);
}

__device__ bool nearest_face(
    float3 point,
    float max_distance,
    const BodyBvh::GpuNode* nodes,
    int node_count,
    const int32_t* leaves,
    const BodyBvh::GpuFace* faces,
    int32_t* out_face,
    float* out_distance) {
    if (node_count <= 0 || !(max_distance >= 0.0F)) {
        return false;
    }
    float best_distance = max_distance;
    int32_t best_face = -1;
    int32_t stack[64];
    int stack_size = 0;
    stack[stack_size++] = 0;
    while (stack_size > 0) {
        const int32_t node_index = stack[--stack_size];
        if (node_index < 0 || node_index >= node_count) {
            continue;
        }
        const BodyBvh::GpuNode& node = nodes[node_index];
        if (aabb_distance(point, node.bmin, node.bmax) > best_distance) {
            continue;
        }
        if (node.leaf != 0) {
            for (int32_t offset = 0; offset < node.count; ++offset) {
                const int32_t leaf_index = leaves[node.first + offset];
                const BodyBvh::GpuFace& face = faces[leaf_index];
                const float3 a = load3(face.a);
                const float3 b = load3(face.b);
                const float3 c = load3(face.c);
                const float3 closest = closest_triangle_point(point, a, b, c);
                const float distance = length3(point - closest);
                if (distance < best_distance) {
                    best_distance = distance;
                    best_face = face.face_index;
                }
            }
        } else {
            const float left_distance =
                aabb_distance(point, nodes[node.left].bmin, nodes[node.left].bmax);
            const float right_distance =
                aabb_distance(point, nodes[node.right].bmin, nodes[node.right].bmax);
            if (left_distance <= right_distance) {
                if (right_distance <= best_distance && stack_size < 64) {
                    stack[stack_size++] = node.right;
                }
                if (left_distance <= best_distance && stack_size < 64) {
                    stack[stack_size++] = node.left;
                }
            } else {
                if (left_distance <= best_distance && stack_size < 64) {
                    stack[stack_size++] = node.left;
                }
                if (right_distance <= best_distance && stack_size < 64) {
                    stack[stack_size++] = node.right;
                }
            }
        }
    }
    if (best_face < 0) {
        return false;
    }
    *out_face = best_face;
    *out_distance = best_distance;
    return true;
}

__device__ void load_face_tri(const float* face_tri, int face_index, float3& a, float3& b, float3& c) {
    const float* base = face_tri + face_index * 9;
    a = make_f3(base[0], base[1], base[2]);
    b = make_f3(base[3], base[4], base[5]);
    c = make_f3(base[6], base[7], base[8]);
}

__global__ void gather_kernel(
    const float* positions,
    const int32_t* locked,
    int32_t vertex_count,
    float search,
    float pad,
    float body_bmin_x,
    float body_bmin_y,
    float body_bmin_z,
    float body_bmax_x,
    float body_bmax_y,
    float body_bmax_z,
    const BodyBvh::GpuNode* nodes,
    int32_t node_count,
    const int32_t* leaves,
    const BodyBvh::GpuFace* faces,
    int32_t face_count,
    int32_t* contact_face,
    int32_t* candidate_count) {
    const int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= vertex_count) {
        return;
    }
    contact_face[index] = -1;
    if (locked[index] != 0) {
        return;
    }
    const float3 point = load_f3(positions, index);
    const float bmin[3] = {body_bmin_x, body_bmin_y, body_bmin_z};
    const float bmax[3] = {body_bmax_x, body_bmax_y, body_bmax_z};
    if (!contains_expanded(point, bmin, bmax, pad)) {
        return;
    }
    // Bounded search only (same contract as host). No infinite-radius fallback.
    int32_t face_index = -1;
    float distance = 0.0F;
    if (!nearest_face(
            point, search, nodes, node_count, leaves, faces, &face_index, &distance)) {
        return;
    }
    if (face_index < 0 || face_index >= face_count) {
        return;
    }
    contact_face[index] = face_index;
    atomicAdd(candidate_count, 1);
}

__global__ void project_kernel(
    float* positions,
    const int32_t* locked,
    int32_t vertex_count,
    float thickness,
    const float* face_tri,
    int32_t face_count,
    const int32_t* contact_face,
    int32_t* hit) {
    const int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= vertex_count) {
        return;
    }
    hit[index] = 0;
    const int32_t face_index = contact_face[index];
    if (face_index < 0 || face_index >= face_count) {
        return;
    }
    if (locked[index] != 0) {
        return;
    }
    float3 point = load_f3(positions, index);
    float3 a, b, c;
    load_face_tri(face_tri, face_index, a, b, c);
    const float3 normal = normalized3(cross3(b - a, c - a));
    const float3 closest = closest_triangle_point(point, a, b, c);
    const float signed_distance = dot3(point - closest, normal);
    if (signed_distance < thickness) {
        float3 correction = normal * (thickness - signed_distance);
        if (signed_distance >= 0.0F) {
            correction = clamp_length3(correction, thickness);
        }
        point += correction;
        store_f3(positions, index, point);
        hit[index] = 1;
    }
}

template <typename T>
T* cuda_alloc_array(size_t count) {
    if (count == 0) {
        return nullptr;
    }
    T* ptr = nullptr;
    YSC_CUDA_CHECK(cudaMalloc(&ptr, count * sizeof(T)));
    return ptr;
}

template <typename T>
void cuda_upload(T* dst, const T* src, size_t count) {
    if (count == 0 || dst == nullptr || src == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(dst, src, count * sizeof(T), cudaMemcpyHostToDevice));
}

}  // namespace

bool BodyContactCuda::device_available() noexcept {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess || count <= 0) {
        return false;
    }
    return true;
}

BodyContactCuda::~BodyContactCuda() {
    destroy();
}

void BodyContactCuda::destroy() noexcept {
    auto free_p = [](auto*& p) {
        if (p != nullptr) {
            cudaFree(p);
            p = nullptr;
        }
    };
    free_p(d_nodes_);
    free_p(d_leaves_);
    free_p(d_bvh_faces_);
    free_p(d_face_tri_);
    free_p(d_pos_);
    free_p(d_locked_);
    free_p(d_contact_face_);
    free_p(d_hit_);
    free_p(d_candidate_count_);
    ready_ = false;
    vertex_count_ = 0;
    node_count_ = 0;
    leaf_count_ = 0;
    face_count_ = 0;
    last_candidate_count_ = 0;
}

bool BodyContactCuda::init(int32_t cloth_vertex_count, const BodyBvh& bvh) {
    destroy();
    if (cloth_vertex_count <= 0 || bvh.empty() || !device_available()) {
        return false;
    }
    try {
        YSC_CUDA_CHECK(cudaSetDevice(0));
        std::vector<BodyBvh::GpuNode> nodes;
        std::vector<int32_t> leaves;
        std::vector<BodyBvh::GpuFace> faces;
        bvh.export_gpu(nodes, leaves, faces);
        if (nodes.empty() || faces.empty()) {
            return false;
        }

        // Dense triangle table keyed by original face_index.
        int32_t max_face = -1;
        for (const auto& f : faces) {
            max_face = std::max(max_face, f.face_index);
        }
        if (max_face < 0) {
            return false;
        }
        std::vector<float> face_tri(static_cast<size_t>(max_face + 1) * 9, 0.0F);
        for (const auto& f : faces) {
            float* base = face_tri.data() + static_cast<size_t>(f.face_index) * 9;
            std::memcpy(base + 0, f.a, 3 * sizeof(float));
            std::memcpy(base + 3, f.b, 3 * sizeof(float));
            std::memcpy(base + 6, f.c, 3 * sizeof(float));
        }

        vertex_count_ = cloth_vertex_count;
        node_count_ = static_cast<int32_t>(nodes.size());
        leaf_count_ = static_cast<int32_t>(leaves.size());
        face_count_ = max_face + 1;
        const Vec3 bmin = bvh.bounds_min();
        const Vec3 bmax = bvh.bounds_max();
        bounds_min_[0] = bmin.x;
        bounds_min_[1] = bmin.y;
        bounds_min_[2] = bmin.z;
        bounds_max_[0] = bmax.x;
        bounds_max_[1] = bmax.y;
        bounds_max_[2] = bmax.z;

        d_nodes_ = cuda_alloc_array<BodyBvh::GpuNode>(nodes.size());
        d_leaves_ = cuda_alloc_array<int32_t>(leaves.size());
        d_bvh_faces_ = cuda_alloc_array<BodyBvh::GpuFace>(faces.size());
        d_face_tri_ = cuda_alloc_array<float>(face_tri.size());
        d_pos_ = cuda_alloc_array<float>(static_cast<size_t>(vertex_count_) * 3);
        d_locked_ = cuda_alloc_array<int32_t>(static_cast<size_t>(vertex_count_));
        d_contact_face_ = cuda_alloc_array<int32_t>(static_cast<size_t>(vertex_count_));
        d_hit_ = cuda_alloc_array<int32_t>(static_cast<size_t>(vertex_count_));
        d_candidate_count_ = cuda_alloc_array<int32_t>(1);

        cuda_upload(d_nodes_, nodes.data(), nodes.size());
        cuda_upload(d_leaves_, leaves.data(), leaves.size());
        cuda_upload(d_bvh_faces_, faces.data(), faces.size());
        cuda_upload(d_face_tri_, face_tri.data(), face_tri.size());
        YSC_CUDA_CHECK(cudaMemset(d_contact_face_, 0xFF, static_cast<size_t>(vertex_count_) * sizeof(int32_t)));
        YSC_CUDA_CHECK(cudaMemset(d_hit_, 0, static_cast<size_t>(vertex_count_) * sizeof(int32_t)));
        ready_ = true;
        return true;
    } catch (...) {
        destroy();
        return false;
    }
}

void BodyContactCuda::upload_cloth(const float* positions_xyz, const int32_t* locked) {
    if (!ready_ || positions_xyz == nullptr || locked == nullptr) {
        return;
    }
    cuda_upload(d_pos_, positions_xyz, static_cast<size_t>(vertex_count_) * 3);
    cuda_upload(d_locked_, locked, static_cast<size_t>(vertex_count_));
}

void BodyContactCuda::download_cloth(float* positions_xyz) const {
    if (!ready_ || positions_xyz == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(
        positions_xyz,
        d_pos_,
        static_cast<size_t>(vertex_count_) * 3 * sizeof(float),
        cudaMemcpyDeviceToHost));
}

void BodyContactCuda::download_hits(int32_t* hits) const {
    if (!ready_ || hits == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(
        hits,
        d_hit_,
        static_cast<size_t>(vertex_count_) * sizeof(int32_t),
        cudaMemcpyDeviceToHost));
}

int32_t BodyContactCuda::project(
    float* external_positions,
    int32_t* external_locked,
    float search_radius,
    float contact_thickness,
    bool gather) {
    if (!ready_) {
        return 0;
    }
    float* positions = external_positions != nullptr ? external_positions : d_pos_;
    int32_t* locked = external_locked != nullptr ? external_locked : d_locked_;
    if (positions == nullptr || locked == nullptr) {
        return 0;
    }

    const int blocks = (vertex_count_ + kThreads - 1) / kThreads;
    const float search = search_radius > contact_thickness ? search_radius : contact_thickness;
    const float pad = search + 1.0e-6F;

    if (gather) {
        YSC_CUDA_CHECK(cudaMemset(d_candidate_count_, 0, sizeof(int32_t)));
        gather_kernel<<<blocks, kThreads>>>(
            positions,
            locked,
            vertex_count_,
            search,
            pad,
            bounds_min_[0],
            bounds_min_[1],
            bounds_min_[2],
            bounds_max_[0],
            bounds_max_[1],
            bounds_max_[2],
            d_nodes_,
            node_count_,
            d_leaves_,
            d_bvh_faces_,
            face_count_,
            d_contact_face_,
            d_candidate_count_);
        YSC_CUDA_CHECK(cudaGetLastError());
        int32_t count = 0;
        YSC_CUDA_CHECK(cudaMemcpy(&count, d_candidate_count_, sizeof(int32_t), cudaMemcpyDeviceToHost));
        last_candidate_count_ = count;
    }

    if (last_candidate_count_ <= 0 && gather) {
        YSC_CUDA_CHECK(cudaMemset(d_hit_, 0, static_cast<size_t>(vertex_count_) * sizeof(int32_t)));
        return 0;
    }
    // When gather==false, still project using previous contact_face_ even if
    // last_candidate_count_ is stale; host mirrors that behaviour with stored faces.

    project_kernel<<<blocks, kThreads>>>(
        positions,
        locked,
        vertex_count_,
        contact_thickness,
        d_face_tri_,
        face_count_,
        d_contact_face_,
        d_hit_);
    YSC_CUDA_CHECK(cudaGetLastError());
    YSC_CUDA_CHECK(cudaDeviceSynchronize());
    return last_candidate_count_;
}

}  // namespace ysc
