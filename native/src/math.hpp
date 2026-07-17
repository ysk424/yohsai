// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace ysc {

constexpr float kEpsilon = 1.0e-8F;

struct Vec3 {
    float x = 0.0F;
    float y = 0.0F;
    float z = 0.0F;

    Vec3() = default;
    Vec3(float x_value, float y_value, float z_value) : x(x_value), y(y_value), z(z_value) {}

    Vec3& operator+=(const Vec3& other) {
        x += other.x;
        y += other.y;
        z += other.z;
        return *this;
    }

    Vec3& operator-=(const Vec3& other) {
        x -= other.x;
        y -= other.y;
        z -= other.z;
        return *this;
    }

    Vec3& operator*=(float scalar) {
        x *= scalar;
        y *= scalar;
        z *= scalar;
        return *this;
    }
};

inline Vec3 operator+(Vec3 left, const Vec3& right) {
    left += right;
    return left;
}

inline Vec3 operator-(Vec3 left, const Vec3& right) {
    left -= right;
    return left;
}

inline Vec3 operator-(const Vec3& value) {
    return {-value.x, -value.y, -value.z};
}

inline Vec3 operator*(Vec3 value, float scalar) {
    value *= scalar;
    return value;
}

inline Vec3 operator*(float scalar, Vec3 value) {
    value *= scalar;
    return value;
}

inline Vec3 operator/(Vec3 value, float scalar) {
    return value * (1.0F / scalar);
}

inline float dot(const Vec3& left, const Vec3& right) {
    return left.x * right.x + left.y * right.y + left.z * right.z;
}

inline Vec3 cross(const Vec3& left, const Vec3& right) {
    return {
        left.y * right.z - left.z * right.y,
        left.z * right.x - left.x * right.z,
        left.x * right.y - left.y * right.x,
    };
}

inline float length_squared(const Vec3& value) {
    return dot(value, value);
}

inline float length(const Vec3& value) {
    return std::sqrt(length_squared(value));
}

inline bool finite(const Vec3& value) {
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

inline Vec3 normalized(const Vec3& value, const Vec3& fallback = {0.0F, 0.0F, 1.0F}) {
    const float magnitude = length(value);
    if (!(magnitude > kEpsilon) || !std::isfinite(magnitude)) {
        return fallback;
    }
    return value / magnitude;
}

inline Vec3 clamp_length(const Vec3& value, float maximum) {
    if (!(maximum > 0.0F)) {
        return value;
    }
    const float magnitude_squared = length_squared(value);
    if (magnitude_squared > maximum * maximum) {
        return value * (maximum / std::sqrt(magnitude_squared));
    }
    return value;
}

struct Quat {
    float w = 1.0F;
    float x = 0.0F;
    float y = 0.0F;
    float z = 0.0F;

    Quat() = default;
    Quat(float w_value, float x_value, float y_value, float z_value)
        : w(w_value), x(x_value), y(y_value), z(z_value) {}

    Quat& operator+=(const Quat& other) {
        w += other.w;
        x += other.x;
        y += other.y;
        z += other.z;
        return *this;
    }

    Quat& operator*=(float scalar) {
        w *= scalar;
        x *= scalar;
        y *= scalar;
        z *= scalar;
        return *this;
    }
};

inline Quat operator+(Quat left, const Quat& right) {
    left += right;
    return left;
}

inline Quat operator-(const Quat& left, const Quat& right) {
    return {left.w - right.w, left.x - right.x, left.y - right.y, left.z - right.z};
}

inline Quat operator*(Quat value, float scalar) {
    value *= scalar;
    return value;
}

inline Quat operator*(float scalar, Quat value) {
    value *= scalar;
    return value;
}

inline Quat operator*(const Quat& left, const Quat& right) {
    return {
        left.w * right.w - left.x * right.x - left.y * right.y - left.z * right.z,
        left.w * right.x + left.x * right.w + left.y * right.z - left.z * right.y,
        left.w * right.y - left.x * right.z + left.y * right.w + left.z * right.x,
        left.w * right.z + left.x * right.y - left.y * right.x + left.z * right.w,
    };
}

inline float dot(const Quat& left, const Quat& right) {
    return left.w * right.w + left.x * right.x + left.y * right.y + left.z * right.z;
}

inline float length_squared(const Quat& value) {
    return dot(value, value);
}

inline float length(const Quat& value) {
    return std::sqrt(length_squared(value));
}

inline bool finite(const Quat& value) {
    return std::isfinite(value.w) && std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

inline Quat conjugate(const Quat& value) {
    return {value.w, -value.x, -value.y, -value.z};
}

inline Quat normalized(const Quat& value, const Quat& fallback = {}) {
    const float magnitude = length(value);
    if (!(magnitude > kEpsilon) || !std::isfinite(magnitude)) {
        return fallback;
    }
    return value * (1.0F / magnitude);
}

inline Quat pure(const Vec3& value) {
    return {0.0F, value.x, value.y, value.z};
}

inline Vec3 vector_part(const Quat& value) {
    return {value.x, value.y, value.z};
}

inline Vec3 rotate(const Quat& rotation, const Vec3& value) {
    const Quat unit = normalized(rotation);
    return vector_part(unit * pure(value) * conjugate(unit));
}

inline Quat from_to(const Vec3& from_value, const Vec3& to_value) {
    const Vec3 from = normalized(from_value);
    const Vec3 to = normalized(to_value);
    const float cosine = std::clamp(dot(from, to), -1.0F, 1.0F);
    if (cosine > 1.0F - 1.0e-6F) {
        return {};
    }
    if (cosine < -1.0F + 1.0e-6F) {
        Vec3 axis = cross(from, {1.0F, 0.0F, 0.0F});
        if (length_squared(axis) < 1.0e-6F) {
            axis = cross(from, {0.0F, 1.0F, 0.0F});
        }
        axis = normalized(axis);
        return {0.0F, axis.x, axis.y, axis.z};
    }
    const Vec3 axis = cross(from, to);
    return normalized(Quat{1.0F + cosine, axis.x, axis.y, axis.z});
}

inline Quat from_basis(const Vec3& d1, const Vec3& d2, const Vec3& d3) {
    const float m00 = d1.x;
    const float m01 = d2.x;
    const float m02 = d3.x;
    const float m10 = d1.y;
    const float m11 = d2.y;
    const float m12 = d3.y;
    const float m20 = d1.z;
    const float m21 = d2.z;
    const float m22 = d3.z;

    Quat result;
    const float trace = m00 + m11 + m22;
    if (trace > 0.0F) {
        const float scale = std::sqrt(trace + 1.0F) * 2.0F;
        result = {
            0.25F * scale,
            (m21 - m12) / scale,
            (m02 - m20) / scale,
            (m10 - m01) / scale,
        };
    } else if (m00 > m11 && m00 > m22) {
        const float scale = std::sqrt(1.0F + m00 - m11 - m22) * 2.0F;
        result = {
            (m21 - m12) / scale,
            0.25F * scale,
            (m01 + m10) / scale,
            (m02 + m20) / scale,
        };
    } else if (m11 > m22) {
        const float scale = std::sqrt(1.0F + m11 - m00 - m22) * 2.0F;
        result = {
            (m02 - m20) / scale,
            (m01 + m10) / scale,
            0.25F * scale,
            (m12 + m21) / scale,
        };
    } else {
        const float scale = std::sqrt(1.0F + m22 - m00 - m11) * 2.0F;
        result = {
            (m10 - m01) / scale,
            (m02 + m20) / scale,
            (m12 + m21) / scale,
            0.25F * scale,
        };
    }
    return normalized(result);
}

inline Vec3 perpendicular_hint(const Vec3& tangent) {
    const Vec3 axis = std::abs(tangent.y) < 0.8F ? Vec3{0.0F, 1.0F, 0.0F} : Vec3{1.0F, 0.0F, 0.0F};
    return normalized(axis - dot(axis, tangent) * tangent, {0.0F, 1.0F, 0.0F});
}

inline Quat frame_from_tangent_normal(const Vec3& tangent_value, const Vec3& normal_value) {
    const Vec3 d3 = normalized(tangent_value);
    Vec3 d2 = normal_value - dot(normal_value, d3) * d3;
    if (length_squared(d2) < 1.0e-8F) {
        d2 = perpendicular_hint(d3);
    } else {
        d2 = normalized(d2);
    }
    const Vec3 d1 = normalized(cross(d2, d3));
    d2 = normalized(cross(d3, d1));
    return from_basis(d1, d2, d3);
}

}  // namespace ysc
