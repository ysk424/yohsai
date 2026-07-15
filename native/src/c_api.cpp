// SPDX-License-Identifier: GPL-3.0-or-later
#include "solver.hpp"

#include <algorithm>
#include <cstring>
#include <exception>
#include <new>
#include <stdexcept>
#include <string>

namespace {

void write_error(char* output, int32_t capacity, const std::string& message) noexcept {
    if (output == nullptr || capacity <= 0) {
        return;
    }
    const size_t available = static_cast<size_t>(capacity - 1);
    const size_t count = std::min(available, message.size());
    std::memcpy(output, message.data(), count);
    output[count] = '\0';
}

void clear_error(char* output, int32_t capacity) noexcept {
    if (output != nullptr && capacity > 0) {
        output[0] = '\0';
    }
}

ysc_status classify_exception(const std::exception& exception) noexcept {
    if (dynamic_cast<const std::invalid_argument*>(&exception) != nullptr) {
        return YSC_STATUS_INVALID_ARGUMENT;
    }
    if (dynamic_cast<const std::out_of_range*>(&exception) != nullptr) {
        return YSC_STATUS_OUT_OF_RANGE;
    }
    const std::string message = exception.what();
    if (message.find("non-finite") != std::string::npos) {
        return YSC_STATUS_NONFINITE_STATE;
    }
    return YSC_STATUS_INTERNAL_ERROR;
}

ysc::Solver& require_solver(ysc_handle handle) {
    if (handle == nullptr) {
        throw std::invalid_argument("solver handle is null");
    }
    return *static_cast<ysc::Solver*>(handle);
}

template <typename Function>
ysc_status guard(char* error_message, int32_t error_capacity, Function&& function) noexcept {
    clear_error(error_message, error_capacity);
    try {
        function();
        return YSC_STATUS_OK;
    } catch (const std::exception& exception) {
        write_error(error_message, error_capacity, exception.what());
        return classify_exception(exception);
    } catch (...) {
        write_error(error_message, error_capacity, "unknown native solver failure");
        return YSC_STATUS_INTERNAL_ERROR;
    }
}

}  // namespace

extern "C" {

int32_t ysc_get_api_version(void) {
    return YSC_API_VERSION;
}

ysc_status ysc_default_config(ysc_config* out_config) {
    if (out_config == nullptr) {
        return YSC_STATUS_INVALID_ARGUMENT;
    }
    *out_config = ysc::default_config();
    return YSC_STATUS_OK;
}

ysc_status ysc_create(
    const ysc_create_desc* desc,
    const ysc_config* config,
    ysc_handle* out_handle,
    char* error_message,
    int32_t error_capacity) {
    if (out_handle != nullptr) {
        *out_handle = nullptr;
    }
    return guard(error_message, error_capacity, [&]() {
        if (desc == nullptr || config == nullptr || out_handle == nullptr) {
            throw std::invalid_argument("ysc_create received a null argument");
        }
        *out_handle = static_cast<ysc_handle>(new ysc::Solver(*desc, *config));
    });
}

void ysc_destroy(ysc_handle handle) {
    delete static_cast<ysc::Solver*>(handle);
}

ysc_status ysc_get_counts(
    ysc_handle handle,
    int32_t* vertex_count,
    int32_t* seam_count,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        if (vertex_count == nullptr || seam_count == nullptr) {
            throw std::invalid_argument("count output pointer is null");
        }
        ysc::Solver& solver = require_solver(handle);
        *vertex_count = solver.vertex_count();
        *seam_count = solver.seam_count();
    });
}

ysc_status ysc_replace_state(
    ysc_handle handle,
    const float* positions,
    const float* velocities,
    const int32_t* locked,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).replace_state(positions, velocities, locked);
    });
}

ysc_status ysc_copy_state(
    ysc_handle handle,
    float* positions,
    float* velocities,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).copy_state(positions, velocities);
    });
}

ysc_status ysc_replace_seam_state(
    ysc_handle handle,
    const float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).replace_seam_state(seam_target_lengths);
    });
}

ysc_status ysc_copy_seam_state(
    ysc_handle handle,
    float* seam_target_lengths,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        require_solver(handle).copy_seam_state(seam_target_lengths);
    });
}

ysc_status ysc_advance(
    ysc_handle handle,
    const ysc_advance_desc* desc,
    ysc_stats* out_stats,
    char* error_message,
    int32_t error_capacity) {
    return guard(error_message, error_capacity, [&]() {
        if (desc == nullptr || out_stats == nullptr) {
            throw std::invalid_argument("advance descriptor or output is null");
        }
        *out_stats = require_solver(handle).advance(*desc);
    });
}

}  // extern "C"
