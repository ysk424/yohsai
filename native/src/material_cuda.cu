// SPDX-License-Identifier: GPL-3.0-or-later
#include "material_cuda.hpp"

#include <cuda_runtime.h>

#include <cmath>
#include <cstring>
#include <stdexcept>
#include <string>

namespace ysc {
namespace {

constexpr float kEpsilon = 1.0e-8F;

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
__device__ inline float3 operator*(float s, float3 a) {
    return a * s;
}
__device__ inline float3& operator+=(float3& a, float3 b) {
    a.x += b.x;
    a.y += b.y;
    a.z += b.z;
    return a;
}
__device__ inline float3& operator-=(float3& a, float3 b) {
    a.x -= b.x;
    a.y -= b.y;
    a.z -= b.z;
    return a;
}
__device__ inline float dot3(float3 a, float3 b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
}
__device__ inline float length3(float3 a) {
    return sqrtf(dot3(a, a));
}
__device__ inline float3 clamp_length3(float3 v, float maximum) {
    if (!(maximum > 0.0F)) {
        return v;
    }
    const float mag2 = dot3(v, v);
    if (mag2 > maximum * maximum) {
        return v * (maximum / sqrtf(mag2));
    }
    return v;
}

__device__ inline float3 load_pos(const float* pos, int32_t i) {
    const int32_t o = i * 3;
    return make_f3(pos[o], pos[o + 1], pos[o + 2]);
}
__device__ inline void store_pos(float* pos, int32_t i, float3 p) {
    const int32_t o = i * 3;
    pos[o] = p.x;
    pos[o + 1] = p.y;
    pos[o + 2] = p.z;
}

__device__ inline float weight_of(const float* inv_mass, const int32_t* locked, int32_t i) {
    if (locked[i] != 0) {
        return 0.0F;
    }
    const float w = inv_mass[i];
    return w > 0.0F ? w : 0.0F;
}

__global__ void k_project_edges(
    float* pos,
    const float* inv_mass,
    const int32_t* locked,
    const int32_t* edge_ab,
    const float* edge_rest,
    const int32_t* indices,
    int32_t begin,
    int32_t count,
    int32_t reverse,
    float stretch_limit,
    float stretch_relaxation,
    float max_corr) {
    const int32_t local = blockIdx.x * blockDim.x + threadIdx.x;
    if (local >= count) {
        return;
    }
    const int32_t index = reverse ? (count - 1 - local) : local;
    const int32_t e = indices[begin + index];
    const int32_t a = edge_ab[e * 2];
    const int32_t b = edge_ab[e * 2 + 1];
    const float rest = edge_rest[e];
    float3 pa = load_pos(pos, a);
    float3 pb = load_pos(pos, b);
    const float3 diff = pb - pa;
    const float current = length3(diff);
    const float slack = rest * stretch_limit;
    const bool hard = current > rest + slack || current < rest - slack;
    const float relaxation = hard ? 1.0F : stretch_relaxation;
    if (!(relaxation > 0.0F) || !(current > kEpsilon)) {
        return;
    }
    const float wa = weight_of(inv_mass, locked, a);
    const float wb = weight_of(inv_mass, locked, b);
    const float wsum = wa + wb;
    if (!(wsum > 0.0F)) {
        return;
    }
    const float3 dir = diff * (1.0F / current);
    const float scaled = relaxation * (current - rest) / wsum;
    if (wa > 0.0F) {
        pa += clamp_length3(dir * (wa * scaled), max_corr);
        store_pos(pos, a, pa);
    }
    if (wb > 0.0F) {
        pb -= clamp_length3(dir * (wb * scaled), max_corr);
        store_pos(pos, b, pb);
    }
}

__global__ void k_project_seams(
    float* pos,
    const float* inv_mass,
    const int32_t* locked,
    const int32_t* seam_ab,
    const float* seam_target,
    const uint8_t* seam_captured,
    const int32_t* indices,
    int32_t begin,
    int32_t count,
    float max_corr) {
    const int32_t local = blockIdx.x * blockDim.x + threadIdx.x;
    if (local >= count) {
        return;
    }
    const int32_t s = indices[begin + local];
    if (seam_captured[s] == 0) {
        return;
    }
    const int32_t a = seam_ab[s * 2];
    const int32_t b = seam_ab[s * 2 + 1];
    const float target = seam_target[s];
    float3 pa = load_pos(pos, a);
    float3 pb = load_pos(pos, b);
    const float wa = weight_of(inv_mass, locked, a);
    const float wb = weight_of(inv_mass, locked, b);
    const float wsum = wa + wb;
    if (!(wsum > 0.0F)) {
        return;
    }
    const float3 diff = pb - pa;
    const float current = length3(diff);
    if (!(current > kEpsilon)) {
        return;
    }
    const float3 dir = diff * (1.0F / current);
    const float scaled = 1.0F * (current - target) / wsum;
    if (wa > 0.0F) {
        pa += clamp_length3(dir * (wa * scaled), max_corr);
        store_pos(pos, a, pa);
    }
    if (wb > 0.0F) {
        pb -= clamp_length3(dir * (wb * scaled), max_corr);
        store_pos(pos, b, pb);
    }
}

__global__ void k_project_quads(
    float* pos,
    const float* inv_mass,
    const int32_t* locked,
    const int32_t* quad_v,
    const float* quad_metric,
    const int32_t* indices,
    int32_t begin,
    int32_t count,
    int32_t reverse,
    float shear_relaxation,
    float max_corr) {
    const int32_t local = blockIdx.x * blockDim.x + threadIdx.x;
    if (local >= count) {
        return;
    }
    const int32_t index = reverse ? (count - 1 - local) : local;
    const int32_t q = indices[begin + index];
    const int32_t v0 = quad_v[q * 4 + 0];
    const int32_t v1 = quad_v[q * 4 + 1];
    const int32_t v2 = quad_v[q * 4 + 2];
    const int32_t v3 = quad_v[q * 4 + 3];
    float3 x0 = load_pos(pos, v0);
    float3 x1 = load_pos(pos, v1);
    float3 x2 = load_pos(pos, v2);
    float3 x3 = load_pos(pos, v3);
    const float w0 = weight_of(inv_mass, locked, v0);
    const float w1 = weight_of(inv_mass, locked, v1);
    const float w2 = weight_of(inv_mass, locked, v2);
    const float w3 = weight_of(inv_mass, locked, v3);
    const float3 u = 0.5F * ((x1 - x0) + (x2 - x3));
    const float3 v = 0.5F * ((x3 - x0) + (x2 - x1));
    const float rest_shear = quad_metric[q * 3 + 2];
    const float value = dot3(u, v) - rest_shear;
    const float3 g0 = -0.5F * (u + v);
    const float3 g1 = 0.5F * (v - u);
    const float3 g2 = 0.5F * (u + v);
    const float3 g3 = 0.5F * (u - v);
    const float denom =
        w0 * dot3(g0, g0) + w1 * dot3(g1, g1) + w2 * dot3(g2, g2) + w3 * dot3(g3, g3);
    if (!(denom > kEpsilon * kEpsilon)) {
        return;
    }
    const float mult = -shear_relaxation * value / denom;
    if (w0 > 0.0F) {
        x0 += clamp_length3(g0 * (w0 * mult), max_corr);
        store_pos(pos, v0, x0);
    }
    if (w1 > 0.0F) {
        x1 += clamp_length3(g1 * (w1 * mult), max_corr);
        store_pos(pos, v1, x1);
    }
    if (w2 > 0.0F) {
        x2 += clamp_length3(g2 * (w2 * mult), max_corr);
        store_pos(pos, v2, x2);
    }
    if (w3 > 0.0F) {
        x3 += clamp_length3(g3 * (w3 * mult), max_corr);
        store_pos(pos, v3, x3);
    }
}

__global__ void k_project_bends(
    float* pos,
    const float* inv_mass,
    const int32_t* locked,
    const int32_t* bend_v,
    const float* bend_rest,
    const int32_t* indices,
    int32_t begin,
    int32_t count,
    int32_t reverse,
    float bend_relaxation,
    float max_corr) {
    const int32_t local = blockIdx.x * blockDim.x + threadIdx.x;
    if (local >= count) {
        return;
    }
    const int32_t index = reverse ? (count - 1 - local) : local;
    const int32_t bidx = indices[begin + index];
    const int32_t ia = bend_v[bidx * 3 + 0];
    const int32_t ib = bend_v[bidx * 3 + 1];
    const int32_t ic = bend_v[bidx * 3 + 2];
    float3 pa = load_pos(pos, ia);
    float3 pb = load_pos(pos, ib);
    float3 pc = load_pos(pos, ic);
    const float wa = weight_of(inv_mass, locked, ia);
    const float wb = weight_of(inv_mass, locked, ib);
    const float wc = weight_of(inv_mass, locked, ic);
    const float prev = bend_rest[bidx * 2 + 0];
    const float next = bend_rest[bidx * 2 + 1];
    const float c0 = 1.0F / prev;
    const float c2 = 1.0F / next;
    const float c1 = -(c0 + c2);
    const float3 curvature = pa * c0 + pb * c1 + pc * c2;
    const float denom = wa * c0 * c0 + wb * c1 * c1 + wc * c2 * c2;
    if (!(denom > kEpsilon)) {
        return;
    }
    if (wa > 0.0F) {
        pa += clamp_length3(curvature * (-bend_relaxation * wa * c0 / denom), max_corr);
        store_pos(pos, ia, pa);
    }
    if (wb > 0.0F) {
        pb += clamp_length3(curvature * (-bend_relaxation * wb * c1 / denom), max_corr);
        store_pos(pos, ib, pb);
    }
    if (wc > 0.0F) {
        pc += clamp_length3(curvature * (-bend_relaxation * wc * c2 / denom), max_corr);
        store_pos(pos, ic, pc);
    }
}

template <typename T>
T* cuda_alloc_array(size_t count) {
    T* ptr = nullptr;
    if (count == 0) {
        return nullptr;
    }
    YSC_CUDA_CHECK(cudaMalloc(&ptr, count * sizeof(T)));
    return ptr;
}

template <typename T>
void cuda_upload(T* dst, const T* src, size_t count) {
    if (count == 0 || dst == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(dst, src, count * sizeof(T), cudaMemcpyHostToDevice));
}

}  // namespace

bool MaterialCuda::device_available() noexcept {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess || count <= 0) {
        return false;
    }
    return true;
}

MaterialCuda::~MaterialCuda() {
    destroy();
}

void MaterialCuda::destroy() noexcept {
    auto free_p = [](auto*& p) {
        if (p != nullptr) {
            cudaFree(p);
            p = nullptr;
        }
    };
    free_p(d_pos_);
    free_p(d_inv_mass_);
    free_p(d_locked_);
    free_p(d_edge_ab_);
    free_p(d_edge_rest_);
    free_p(d_edge_colour_offsets_);
    free_p(d_edge_colour_indices_);
    free_p(d_quad_v_);
    free_p(d_quad_metric_);
    free_p(d_quad_colour_offsets_);
    free_p(d_quad_colour_indices_);
    free_p(d_bend_v_);
    free_p(d_bend_rest_);
    free_p(d_bend_colour_offsets_);
    free_p(d_bend_colour_indices_);
    free_p(d_seam_ab_);
    free_p(d_seam_target_);
    free_p(d_seam_captured_);
    free_p(d_seam_colour_offsets_);
    free_p(d_seam_colour_indices_);
    ready_ = false;
}

bool MaterialCuda::init(
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
    const std::vector<int32_t>& seam_colour_indices) {
    destroy();
    if (!device_available() || vertex_count <= 0) {
        return false;
    }
    try {
        YSC_CUDA_CHECK(cudaSetDevice(0));
        vertex_count_ = vertex_count;
        edge_count_ = static_cast<int32_t>(edges.size());
        quad_count_ = static_cast<int32_t>(quads.size());
        bend_count_ = static_cast<int32_t>(bends.size());
        seam_count_ = static_cast<int32_t>(seams.size());

        d_pos_ = cuda_alloc_array<float>(static_cast<size_t>(vertex_count_) * 3);
        d_inv_mass_ = cuda_alloc_array<float>(static_cast<size_t>(vertex_count_));
        d_locked_ = cuda_alloc_array<int32_t>(static_cast<size_t>(vertex_count_));
        cuda_upload(d_inv_mass_, inverse_masses, static_cast<size_t>(vertex_count_));
        cuda_upload(d_locked_, locked, static_cast<size_t>(vertex_count_));

        if (edge_count_ > 0) {
            std::vector<int32_t> ab(static_cast<size_t>(edge_count_) * 2);
            std::vector<float> rest(static_cast<size_t>(edge_count_));
            for (int32_t i = 0; i < edge_count_; ++i) {
                ab[static_cast<size_t>(i) * 2] = edges[static_cast<size_t>(i)].a;
                ab[static_cast<size_t>(i) * 2 + 1] = edges[static_cast<size_t>(i)].b;
                rest[static_cast<size_t>(i)] = edges[static_cast<size_t>(i)].rest_length;
            }
            d_edge_ab_ = cuda_alloc_array<int32_t>(ab.size());
            d_edge_rest_ = cuda_alloc_array<float>(rest.size());
            cuda_upload(d_edge_ab_, ab.data(), ab.size());
            cuda_upload(d_edge_rest_, rest.data(), rest.size());
        }
        edge_colour_offsets_host_ = edge_colour_offsets;
        edge_colour_count_ = static_cast<int32_t>(edge_colour_offsets.size()) - 1;
        if (edge_colour_count_ < 0) {
            edge_colour_count_ = 0;
        }
        if (!edge_colour_offsets.empty()) {
            d_edge_colour_offsets_ = cuda_alloc_array<int32_t>(edge_colour_offsets.size());
            cuda_upload(d_edge_colour_offsets_, edge_colour_offsets.data(), edge_colour_offsets.size());
        }
        if (!edge_colour_indices.empty()) {
            d_edge_colour_indices_ = cuda_alloc_array<int32_t>(edge_colour_indices.size());
            cuda_upload(d_edge_colour_indices_, edge_colour_indices.data(), edge_colour_indices.size());
        }

        if (quad_count_ > 0) {
            std::vector<int32_t> vv(static_cast<size_t>(quad_count_) * 4);
            std::vector<float> metric(static_cast<size_t>(quad_count_) * 3);
            for (int32_t i = 0; i < quad_count_; ++i) {
                const Quad& q = quads[static_cast<size_t>(i)];
                vv[static_cast<size_t>(i) * 4 + 0] = q.v0;
                vv[static_cast<size_t>(i) * 4 + 1] = q.v1;
                vv[static_cast<size_t>(i) * 4 + 2] = q.v2;
                vv[static_cast<size_t>(i) * 4 + 3] = q.v3;
                metric[static_cast<size_t>(i) * 3 + 0] = q.rest_u_squared;
                metric[static_cast<size_t>(i) * 3 + 1] = q.rest_v_squared;
                metric[static_cast<size_t>(i) * 3 + 2] = q.rest_shear;
            }
            d_quad_v_ = cuda_alloc_array<int32_t>(vv.size());
            d_quad_metric_ = cuda_alloc_array<float>(metric.size());
            cuda_upload(d_quad_v_, vv.data(), vv.size());
            cuda_upload(d_quad_metric_, metric.data(), metric.size());
        }
        quad_colour_offsets_host_ = quad_colour_offsets;
        quad_colour_count_ = static_cast<int32_t>(quad_colour_offsets.size()) - 1;
        if (quad_colour_count_ < 0) {
            quad_colour_count_ = 0;
        }
        if (!quad_colour_offsets.empty()) {
            d_quad_colour_offsets_ = cuda_alloc_array<int32_t>(quad_colour_offsets.size());
            cuda_upload(d_quad_colour_offsets_, quad_colour_offsets.data(), quad_colour_offsets.size());
        }
        if (!quad_colour_indices.empty()) {
            d_quad_colour_indices_ = cuda_alloc_array<int32_t>(quad_colour_indices.size());
            cuda_upload(d_quad_colour_indices_, quad_colour_indices.data(), quad_colour_indices.size());
        }

        if (bend_count_ > 0) {
            std::vector<int32_t> vv(static_cast<size_t>(bend_count_) * 3);
            std::vector<float> rest(static_cast<size_t>(bend_count_) * 2);
            for (int32_t i = 0; i < bend_count_; ++i) {
                const Bend& b = bends[static_cast<size_t>(i)];
                vv[static_cast<size_t>(i) * 3 + 0] = b.a;
                vv[static_cast<size_t>(i) * 3 + 1] = b.b;
                vv[static_cast<size_t>(i) * 3 + 2] = b.c;
                rest[static_cast<size_t>(i) * 2 + 0] = b.previous_rest;
                rest[static_cast<size_t>(i) * 2 + 1] = b.next_rest;
            }
            d_bend_v_ = cuda_alloc_array<int32_t>(vv.size());
            d_bend_rest_ = cuda_alloc_array<float>(rest.size());
            cuda_upload(d_bend_v_, vv.data(), vv.size());
            cuda_upload(d_bend_rest_, rest.data(), rest.size());
        }
        bend_colour_offsets_host_ = bend_colour_offsets;
        bend_colour_count_ = static_cast<int32_t>(bend_colour_offsets.size()) - 1;
        if (bend_colour_count_ < 0) {
            bend_colour_count_ = 0;
        }
        if (!bend_colour_offsets.empty()) {
            d_bend_colour_offsets_ = cuda_alloc_array<int32_t>(bend_colour_offsets.size());
            cuda_upload(d_bend_colour_offsets_, bend_colour_offsets.data(), bend_colour_offsets.size());
        }
        if (!bend_colour_indices.empty()) {
            d_bend_colour_indices_ = cuda_alloc_array<int32_t>(bend_colour_indices.size());
            cuda_upload(d_bend_colour_indices_, bend_colour_indices.data(), bend_colour_indices.size());
        }

        if (seam_count_ > 0) {
            std::vector<int32_t> ab(static_cast<size_t>(seam_count_) * 2);
            std::vector<float> target(static_cast<size_t>(seam_count_));
            for (int32_t i = 0; i < seam_count_; ++i) {
                ab[static_cast<size_t>(i) * 2] = seams[static_cast<size_t>(i)].a;
                ab[static_cast<size_t>(i) * 2 + 1] = seams[static_cast<size_t>(i)].b;
                target[static_cast<size_t>(i)] = seams[static_cast<size_t>(i)].target_length;
            }
            d_seam_ab_ = cuda_alloc_array<int32_t>(ab.size());
            d_seam_target_ = cuda_alloc_array<float>(target.size());
            d_seam_captured_ = cuda_alloc_array<uint8_t>(static_cast<size_t>(seam_count_));
            cuda_upload(d_seam_ab_, ab.data(), ab.size());
            cuda_upload(d_seam_target_, target.data(), target.size());
            YSC_CUDA_CHECK(cudaMemset(d_seam_captured_, 0, static_cast<size_t>(seam_count_)));
        }
        seam_colour_offsets_host_ = seam_colour_offsets;
        seam_colour_count_ = static_cast<int32_t>(seam_colour_offsets.size()) - 1;
        if (seam_colour_count_ < 0) {
            seam_colour_count_ = 0;
        }
        if (!seam_colour_offsets.empty()) {
            d_seam_colour_offsets_ = cuda_alloc_array<int32_t>(seam_colour_offsets.size());
            cuda_upload(d_seam_colour_offsets_, seam_colour_offsets.data(), seam_colour_offsets.size());
        }
        if (!seam_colour_indices.empty()) {
            d_seam_colour_indices_ = cuda_alloc_array<int32_t>(seam_colour_indices.size());
            cuda_upload(d_seam_colour_indices_, seam_colour_indices.data(), seam_colour_indices.size());
        }

        ready_ = true;
        return true;
    } catch (...) {
        destroy();
        return false;
    }
}

void MaterialCuda::upload_positions(const float* positions_xyz) {
    if (!ready_ || positions_xyz == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(
        d_pos_,
        positions_xyz,
        static_cast<size_t>(vertex_count_) * 3 * sizeof(float),
        cudaMemcpyHostToDevice));
}

void MaterialCuda::download_positions(float* positions_xyz) const {
    if (!ready_ || positions_xyz == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(
        positions_xyz,
        d_pos_,
        static_cast<size_t>(vertex_count_) * 3 * sizeof(float),
        cudaMemcpyDeviceToHost));
}

void MaterialCuda::upload_locked(const int32_t* locked) {
    if (!ready_ || locked == nullptr || d_locked_ == nullptr) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(
        d_locked_,
        locked,
        static_cast<size_t>(vertex_count_) * sizeof(int32_t),
        cudaMemcpyHostToDevice));
}

void MaterialCuda::upload_seam_captured(const uint8_t* captured, int32_t seam_count) {
    if (!ready_ || d_seam_captured_ == nullptr || captured == nullptr || seam_count != seam_count_) {
        return;
    }
    YSC_CUDA_CHECK(cudaMemcpy(
        d_seam_captured_,
        captured,
        static_cast<size_t>(seam_count_) * sizeof(uint8_t),
        cudaMemcpyHostToDevice));
}

void MaterialCuda::project_materials(bool reverse) {
    if (!ready_) {
        return;
    }
    constexpr int threads = 128;

    auto run_seams = [&]() {
        for (int32_t colour = 0; colour < seam_colour_count_; ++colour) {
            const int32_t begin = seam_colour_offsets_host_[static_cast<size_t>(colour)];
            const int32_t end = seam_colour_offsets_host_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
            if (count <= 0) {
                continue;
            }
            const int blocks = (count + threads - 1) / threads;
            k_project_seams<<<blocks, threads>>>(
                d_pos_,
                d_inv_mass_,
                d_locked_,
                d_seam_ab_,
                d_seam_target_,
                d_seam_captured_,
                d_seam_colour_indices_,
                begin,
                count,
                params_.maximum_position_correction);
            YSC_CUDA_CHECK(cudaGetLastError());
        }
    };

    auto run_quads = [&]() {
        for (int32_t offset = 0; offset < quad_colour_count_; ++offset) {
            const int32_t colour = reverse ? (quad_colour_count_ - 1 - offset) : offset;
            const int32_t begin = quad_colour_offsets_host_[static_cast<size_t>(colour)];
            const int32_t end = quad_colour_offsets_host_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
            if (count <= 0) {
                continue;
            }
            const int blocks = (count + threads - 1) / threads;
            k_project_quads<<<blocks, threads>>>(
                d_pos_,
                d_inv_mass_,
                d_locked_,
                d_quad_v_,
                d_quad_metric_,
                d_quad_colour_indices_,
                begin,
                count,
                reverse ? 1 : 0,
                params_.shear_relaxation,
                params_.maximum_position_correction);
            YSC_CUDA_CHECK(cudaGetLastError());
        }
    };

    auto run_bends = [&]() {
        for (int32_t offset = 0; offset < bend_colour_count_; ++offset) {
            const int32_t colour = reverse ? (bend_colour_count_ - 1 - offset) : offset;
            const int32_t begin = bend_colour_offsets_host_[static_cast<size_t>(colour)];
            const int32_t end = bend_colour_offsets_host_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
            if (count <= 0) {
                continue;
            }
            const int blocks = (count + threads - 1) / threads;
            k_project_bends<<<blocks, threads>>>(
                d_pos_,
                d_inv_mass_,
                d_locked_,
                d_bend_v_,
                d_bend_rest_,
                d_bend_colour_indices_,
                begin,
                count,
                reverse ? 1 : 0,
                params_.bend_relaxation,
                params_.maximum_position_correction);
            YSC_CUDA_CHECK(cudaGetLastError());
        }
    };

    auto run_edges = [&](bool edge_reverse) {
        for (int32_t offset = 0; offset < edge_colour_count_; ++offset) {
            const int32_t colour = edge_reverse ? (edge_colour_count_ - 1 - offset) : offset;
            const int32_t begin = edge_colour_offsets_host_[static_cast<size_t>(colour)];
            const int32_t end = edge_colour_offsets_host_[static_cast<size_t>(colour + 1)];
            const int32_t count = end - begin;
            if (count <= 0) {
                continue;
            }
            const int blocks = (count + threads - 1) / threads;
            k_project_edges<<<blocks, threads>>>(
                d_pos_,
                d_inv_mass_,
                d_locked_,
                d_edge_ab_,
                d_edge_rest_,
                d_edge_colour_indices_,
                begin,
                count,
                edge_reverse ? 1 : 0,
                params_.stretch_limit,
                params_.stretch_relaxation,
                params_.maximum_position_correction);
            YSC_CUDA_CHECK(cudaGetLastError());
        }
    };

    // Same order as CPU advance materials: seams, shear, bends, edges, edges.
    run_seams();
    run_quads();
    run_bends();
    run_edges(reverse);
    run_edges(!reverse);
    YSC_CUDA_CHECK(cudaDeviceSynchronize());
}

}  // namespace ysc
