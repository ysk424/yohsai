// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "math.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

namespace ysc {

// Axis-aligned BVH over body triangles for nearest-face queries.
// Built once at solver create; queried with OpenMP from cloth vertices.
class BodyBvh {
public:
    struct FaceRef {
        int32_t face_index = 0;
        Vec3 a{};
        Vec3 b{};
        Vec3 c{};
        Vec3 centroid{};
    };

    BodyBvh() = default;

    void build(const std::vector<Vec3>& positions, const std::vector<std::array<int32_t, 3>>& faces) {
        nodes_.clear();
        leaves_.clear();
        face_refs_.clear();
        face_refs_.reserve(faces.size());
        for (int32_t index = 0; index < static_cast<int32_t>(faces.size()); ++index) {
            const auto& face = faces[static_cast<size_t>(index)];
            FaceRef ref{};
            ref.face_index = index;
            ref.a = positions[static_cast<size_t>(face[0])];
            ref.b = positions[static_cast<size_t>(face[1])];
            ref.c = positions[static_cast<size_t>(face[2])];
            ref.centroid = (ref.a + ref.b + ref.c) * (1.0F / 3.0F);
            face_refs_.push_back(ref);
        }
        if (face_refs_.empty()) {
            bounds_min_ = {};
            bounds_max_ = {};
            return;
        }
        std::vector<int32_t> order(face_refs_.size());
        for (int32_t index = 0; index < static_cast<int32_t>(order.size()); ++index) {
            order[static_cast<size_t>(index)] = index;
        }
        nodes_.reserve(face_refs_.size() * 2);
        build_range(order.data(), static_cast<int32_t>(order.size()));
        bounds_min_ = nodes_[0].bmin;
        bounds_max_ = nodes_[0].bmax;
    }

    [[nodiscard]] bool empty() const noexcept { return face_refs_.empty(); }

    [[nodiscard]] Vec3 bounds_min() const noexcept { return bounds_min_; }
    [[nodiscard]] Vec3 bounds_max() const noexcept { return bounds_max_; }

    // Flat tables for uploading a static Body BVH to CUDA (body is fixed).
    struct GpuNode {
        float bmin[3];
        float bmax[3];
        int32_t left;
        int32_t right;
        int32_t first;
        int32_t count;
        int32_t leaf;  // 0/1
        int32_t _pad;
    };
    struct GpuFace {
        float a[3];
        float b[3];
        float c[3];
        int32_t face_index;
        int32_t _pad[3];  // 36 + 4 + 12 = 48 (host/device layout match)
    };

    void export_gpu(
        std::vector<GpuNode>& nodes,
        std::vector<int32_t>& leaves,
        std::vector<GpuFace>& faces) const {
        nodes.resize(nodes_.size());
        for (size_t i = 0; i < nodes_.size(); ++i) {
            const Node& n = nodes_[i];
            GpuNode& g = nodes[i];
            g.bmin[0] = n.bmin.x;
            g.bmin[1] = n.bmin.y;
            g.bmin[2] = n.bmin.z;
            g.bmax[0] = n.bmax.x;
            g.bmax[1] = n.bmax.y;
            g.bmax[2] = n.bmax.z;
            g.left = n.left;
            g.right = n.right;
            g.first = n.first;
            g.count = n.count;
            g.leaf = n.leaf ? 1 : 0;
            g._pad = 0;
        }
        leaves = leaves_;
        faces.resize(face_refs_.size());
        for (size_t i = 0; i < face_refs_.size(); ++i) {
            const FaceRef& f = face_refs_[i];
            GpuFace& g = faces[i];
            g.a[0] = f.a.x;
            g.a[1] = f.a.y;
            g.a[2] = f.a.z;
            g.b[0] = f.b.x;
            g.b[1] = f.b.y;
            g.b[2] = f.b.z;
            g.c[0] = f.c.x;
            g.c[1] = f.c.y;
            g.c[2] = f.c.z;
            g.face_index = f.face_index;
            g._pad[0] = g._pad[1] = g._pad[2] = 0;
        }
    }

    // Nearest triangle face index within max_distance. Returns false if none.
    [[nodiscard]] bool nearest_face(
        const Vec3& point,
        float max_distance,
        int32_t* out_face,
        float* out_distance) const {
        if (empty() || out_face == nullptr || out_distance == nullptr || !(max_distance >= 0.0F)) {
            return false;
        }
        float best_distance = max_distance;
        int32_t best_face = -1;
        int32_t stack[64];
        int32_t stack_size = 0;
        stack[stack_size++] = 0;
        while (stack_size > 0) {
            const int32_t node_index = stack[--stack_size];
            const Node& node = nodes_[static_cast<size_t>(node_index)];
            if (aabb_distance(point, node.bmin, node.bmax) > best_distance) {
                continue;
            }
            if (node.leaf) {
                for (int32_t offset = 0; offset < node.count; ++offset) {
                    const FaceRef& face = face_refs_[static_cast<size_t>(
                        leaves_[static_cast<size_t>(node.first + offset)])];
                    const float distance = std::sqrt(point_triangle_distance_squared(point, face.a, face.b, face.c));
                    if (distance < best_distance) {
                        best_distance = distance;
                        best_face = face.face_index;
                    }
                }
            } else {
                // Visit nearer child first for better pruning.
                const float left_distance =
                    aabb_distance(point, nodes_[static_cast<size_t>(node.left)].bmin, nodes_[static_cast<size_t>(node.left)].bmax);
                const float right_distance =
                    aabb_distance(point, nodes_[static_cast<size_t>(node.right)].bmin, nodes_[static_cast<size_t>(node.right)].bmax);
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

    [[nodiscard]] bool contains_expanded(const Vec3& point, float padding) const noexcept {
        return point.x >= bounds_min_.x - padding && point.x <= bounds_max_.x + padding &&
            point.y >= bounds_min_.y - padding && point.y <= bounds_max_.y + padding &&
            point.z >= bounds_min_.z - padding && point.z <= bounds_max_.z + padding;
    }

    static float point_triangle_distance_squared(
        const Vec3& point,
        const Vec3& a,
        const Vec3& b,
        const Vec3& c) {
        const Vec3 closest = closest_triangle_point(point, a, b, c);
        return length_squared(point - closest);
    }

    static Vec3 closest_triangle_point(const Vec3& point, const Vec3& a, const Vec3& b, const Vec3& c) {
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

private:
    struct Node {
        Vec3 bmin{};
        Vec3 bmax{};
        int32_t left = -1;
        int32_t right = -1;
        int32_t first = 0;
        int32_t count = 0;
        bool leaf = false;
    };

    static constexpr int32_t kLeafSize = 8;

    std::vector<Node> nodes_;
    std::vector<int32_t> leaves_;
    std::vector<FaceRef> face_refs_;
    Vec3 bounds_min_{};
    Vec3 bounds_max_{};

    static float aabb_distance(const Vec3& point, const Vec3& bmin, const Vec3& bmax) {
        const float dx = point.x < bmin.x ? bmin.x - point.x : (point.x > bmax.x ? point.x - bmax.x : 0.0F);
        const float dy = point.y < bmin.y ? bmin.y - point.y : (point.y > bmax.y ? point.y - bmax.y : 0.0F);
        const float dz = point.z < bmin.z ? bmin.z - point.z : (point.z > bmax.z ? point.z - bmax.z : 0.0F);
        return std::sqrt(dx * dx + dy * dy + dz * dz);
    }

    static void bounds_of(
        const FaceRef* refs,
        const int32_t* order,
        int32_t count,
        Vec3& bmin,
        Vec3& bmax) {
        bmin = {
            std::numeric_limits<float>::infinity(),
            std::numeric_limits<float>::infinity(),
            std::numeric_limits<float>::infinity(),
        };
        bmax = {
            -std::numeric_limits<float>::infinity(),
            -std::numeric_limits<float>::infinity(),
            -std::numeric_limits<float>::infinity(),
        };
        for (int32_t index = 0; index < count; ++index) {
            const FaceRef& face = refs[order[index]];
            for (const Vec3& p : {face.a, face.b, face.c}) {
                bmin.x = std::min(bmin.x, p.x);
                bmin.y = std::min(bmin.y, p.y);
                bmin.z = std::min(bmin.z, p.z);
                bmax.x = std::max(bmax.x, p.x);
                bmax.y = std::max(bmax.y, p.y);
                bmax.z = std::max(bmax.z, p.z);
            }
        }
    }

    int32_t build_range(int32_t* order, int32_t count) {
        const int32_t node_index = static_cast<int32_t>(nodes_.size());
        nodes_.push_back({});
        Node node{};
        bounds_of(face_refs_.data(), order, count, node.bmin, node.bmax);
        if (count <= kLeafSize) {
            node.leaf = true;
            node.first = static_cast<int32_t>(leaves_.size());
            node.count = count;
            for (int32_t index = 0; index < count; ++index) {
                leaves_.push_back(order[index]);
            }
            nodes_[static_cast<size_t>(node_index)] = node;
            return node_index;
        }
        const Vec3 extent = node.bmax - node.bmin;
        int32_t axis = 0;
        if (extent.y > extent.x && extent.y >= extent.z) {
            axis = 1;
        } else if (extent.z > extent.x && extent.z >= extent.y) {
            axis = 2;
        }
        const int32_t mid = count / 2;
        std::nth_element(order, order + mid, order + count, [&](int32_t left, int32_t right) {
            const Vec3& lc = face_refs_[static_cast<size_t>(left)].centroid;
            const Vec3& rc = face_refs_[static_cast<size_t>(right)].centroid;
            const float la = axis == 0 ? lc.x : (axis == 1 ? lc.y : lc.z);
            const float ra = axis == 0 ? rc.x : (axis == 1 ? rc.y : rc.z);
            return la < ra;
        });
        // Prevent empty split on equal centroids.
        int32_t split = mid;
        if (split <= 0) {
            split = 1;
        }
        if (split >= count) {
            split = count - 1;
        }
        node.leaf = false;
        node.left = build_range(order, split);
        node.right = build_range(order + split, count - split);
        nodes_[static_cast<size_t>(node_index)] = node;
        return node_index;
    }
};

}  // namespace ysc
