# SPDX-License-Identifier: GPL-3.0-or-later

option(YSC_ENABLE_CUDA "Build CUDA material projections and Body BVH contact" ON)

set(YSC_SOLVER_SOURCES
    src/c_api.cpp
    src/solver.cpp
)

if(YSC_ENABLE_CUDA)
    include(CheckLanguage)
    check_language(CUDA)
    if(CMAKE_CUDA_COMPILER)
        enable_language(CUDA)
        list(APPEND YSC_SOLVER_SOURCES src/material_cuda.cu src/body_contact_cuda.cu)
        message(STATUS "Yohsai: CUDA material + Body contact ENABLED (${CMAKE_CUDA_COMPILER})")
    else()
        set(YSC_ENABLE_CUDA OFF)
        message(WARNING "Yohsai: CUDA requested but no compiler found; building OpenMP-only")
    endif()
endif()

add_library(yohsai_solver SHARED ${YSC_SOLVER_SOURCES})

find_package(OpenMP COMPONENTS CXX)
if(MSVC)
    if(NOT OpenMP_CXX_FOUND)
        message(FATAL_ERROR "MSVC OpenMP is required for the Windows solver")
    endif()
    # MSVC OpenMP links VCOMP140.DLL dynamically. Install the licensed x64
    # redistributable beside the solver so a portable Blender installation
    # does not depend on an unrelated system-wide VC runtime installation.
    set(CMAKE_INSTALL_OPENMP_LIBRARIES TRUE)
    set(CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS_SKIP TRUE)
    include(InstallRequiredSystemLibraries)
    set(YSC_OPENMP_RUNTIME_LIBS "")
    foreach(runtime_library IN LISTS CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS)
        if(runtime_library MATCHES "[/\\\\]vcomp140\\.dll$")
            list(APPEND YSC_OPENMP_RUNTIME_LIBS "${runtime_library}")
        endif()
    endforeach()
    if(NOT YSC_OPENMP_RUNTIME_LIBS)
        message(FATAL_ERROR "MSVC OpenMP runtime vcomp140.dll was not found")
    endif()
    install(PROGRAMS ${YSC_OPENMP_RUNTIME_LIBS} DESTINATION bin)
endif()

target_include_directories(yohsai_solver
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_compile_features(yohsai_solver PUBLIC cxx_std_20)
target_compile_definitions(yohsai_solver PRIVATE YSC_BUILD_DLL)
if(OpenMP_CXX_FOUND)
    target_link_libraries(yohsai_solver PRIVATE OpenMP::OpenMP_CXX)
endif()

if(YSC_ENABLE_CUDA AND CMAKE_CUDA_COMPILER)
    find_package(CUDAToolkit REQUIRED)
    target_compile_definitions(yohsai_solver PRIVATE YSC_ENABLE_CUDA=1)
    target_link_libraries(yohsai_solver PRIVATE CUDA::cudart_static)
    set_target_properties(yohsai_solver PROPERTIES
        CUDA_SEPARABLE_COMPILATION ON
        CUDA_RUNTIME_LIBRARY Static
        # 5070 Ti is sm_120; keep 89 for Ada fallback machines.
        CUDA_ARCHITECTURES "89-real;120-real;89-virtual"
    )
    if(MSVC)
        target_compile_options(yohsai_solver PRIVATE
            $<$<COMPILE_LANGUAGE:CUDA>:-O3;--use_fast_math;-Xcompiler=/openmp>
        )
        # cudart_static may pull static CRT; keep dynamic for Blender.
        target_link_options(yohsai_solver PRIVATE /NODEFAULTLIB:LIBCMT)
    else()
        target_compile_options(yohsai_solver PRIVATE
            $<$<COMPILE_LANGUAGE:CUDA>:-O3;--use_fast_math>
        )
    endif()
endif()

if(MSVC)
    target_compile_options(yohsai_solver PRIVATE
        $<$<COMPILE_LANGUAGE:CXX>:/W4;/permissive-;/EHsc;/openmp>
    )
else()
    target_compile_options(yohsai_solver PRIVATE
        $<$<COMPILE_LANGUAGE:CXX>:-Wall;-Wextra;-Wpedantic>
    )
endif()

set_target_properties(yohsai_solver PROPERTIES
    OUTPUT_NAME "yohsai_solver"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

install(TARGETS yohsai_solver
    RUNTIME DESTINATION bin
    LIBRARY DESTINATION bin
)
