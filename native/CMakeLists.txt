# SPDX-License-Identifier: GPL-3.0-or-later

add_library(yohsai_solver SHARED
    src/c_api.cpp
    src/solver.cpp
)

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

if(MSVC)
    target_compile_options(yohsai_solver PRIVATE /W4 /permissive- /EHsc /openmp)
else()
    target_compile_options(yohsai_solver PRIVATE -Wall -Wextra -Wpedantic)
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
