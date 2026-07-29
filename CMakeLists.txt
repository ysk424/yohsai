# SPDX-License-Identifier: GPL-3.0-or-later

cmake_minimum_required(VERSION 3.24)

# CUDA is enabled inside native/ when YSC_ENABLE_CUDA and a compiler exist.
project(yohsai_solver VERSION 0.9.3 LANGUAGES CXX)

add_subdirectory(native)
