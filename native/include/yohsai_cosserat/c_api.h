// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <stdint.h>

#if defined(_WIN32)
#  if defined(YSC_BUILD_DLL)
#    define YSC_API __declspec(dllexport)
#  else
#    define YSC_API __declspec(dllimport)
#  endif
#else
#  define YSC_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define YSC_API_VERSION 7

typedef void* ysc_handle;

typedef enum ysc_status {
    YSC_STATUS_OK = 0,
    YSC_STATUS_INVALID_ARGUMENT = 1,
    YSC_STATUS_OUT_OF_RANGE = 2,
    YSC_STATUS_NONFINITE_STATE = 3,
    YSC_STATUS_INTERNAL_ERROR = 4
} ysc_status;

typedef struct ysc_config {
    float time_step;
    int32_t substeps;
    int32_t iterations;
    /* Constant attraction magnitude for one unit-inverse-mass endpoint. */
    float seam_attraction_force;
    float seam_capture_distance;
    /* Per-iteration material energy-projection fractions in [0, 1]. */
    float stretch_relaxation;
    float shear_relaxation;
    float bend_relaxation;
    float maximum_position_correction;
    float contact_thickness;
} ysc_config;

typedef struct ysc_create_desc {
    int32_t vertex_count;
    const float* positions;
    const float* velocities;
    const float* inverse_masses;
    const int32_t* locked;

    int32_t seam_count;
    const int32_t* seams;

    int32_t edge_count;
    const int32_t* edges;
    const float* edge_rest_lengths;

    int32_t quad_count;
    const int32_t* quads;
    /* Per quad: rest dot(u,u), dot(v,v), dot(u,v). */
    const float* quad_rest_metrics;

    int32_t bend_count;
    /* Per bend: previous, center, next vertex along one material axis. */
    const int32_t* bends;
    /* Per bend: the two positive rest segment lengths. */
    const float* bend_rest_lengths;

    int32_t body_vertex_count;
    const float* body_positions;
    int32_t body_face_count;
    const int32_t* body_faces;
} ysc_create_desc;

typedef struct ysc_advance_desc {
    float gravity[3];
    int32_t iterations;
    int32_t body_candidate_count;
    const int32_t* body_candidates;
} ysc_advance_desc;

typedef struct ysc_stats {
    int32_t substeps;
    int32_t iterations;
    int32_t seam_count;
    int32_t captured_seam_count;
    int32_t edge_count;
    int32_t quad_count;
    int32_t bend_count;
    int32_t body_candidate_count;
    float maximum_displacement;
} ysc_stats;

YSC_API int32_t ysc_get_api_version(void);
YSC_API ysc_status ysc_default_config(ysc_config* out_config);

YSC_API ysc_status ysc_create(
    const ysc_create_desc* desc,
    const ysc_config* config,
    ysc_handle* out_handle,
    char* error_message,
    int32_t error_capacity);

YSC_API void ysc_destroy(ysc_handle handle);

YSC_API ysc_status ysc_get_counts(
    ysc_handle handle,
    int32_t* vertex_count,
    int32_t* seam_count,
    char* error_message,
    int32_t error_capacity);

YSC_API ysc_status ysc_replace_state(
    ysc_handle handle,
    const float* positions,
    const float* velocities,
    const int32_t* locked,
    char* error_message,
    int32_t error_capacity);

YSC_API ysc_status ysc_copy_state(
    ysc_handle handle,
    float* positions,
    float* velocities,
    char* error_message,
    int32_t error_capacity);

YSC_API ysc_status ysc_replace_seam_state(
    ysc_handle handle,
    const float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity);

YSC_API ysc_status ysc_copy_seam_state(
    ysc_handle handle,
    float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity);

YSC_API ysc_status ysc_advance(
    ysc_handle handle,
    const ysc_advance_desc* desc,
    ysc_stats* out_stats,
    char* error_message,
    int32_t error_capacity);

#ifdef __cplusplus
}
#endif
