cmake_minimum_required(VERSION 3.16.3)

project(gvtest)

install(DIRECTORY bin/ DESTINATION bin USE_SOURCE_PERMISSIONS)
install(DIRECTORY python/ DESTINATION python)
