# OpenModelica toolchain for generating FMUs from World Model fragments (Phase 1 spike, #13).
# Libraries are pinned by tag and laid out as "<Name> <version>" so loadModel(X, {"ver"}) works.
FROM openmodelica/openmodelica:v1.25.0-minimal

ARG MSL_TAG=v4.0.0
ARG BUILDINGS_TAG=v11.1.0

RUN mkdir -p /opt/omlib /tmp/src && cd /tmp/src \
 && wget -q -O msl.tgz "https://github.com/modelica/ModelicaStandardLibrary/archive/refs/tags/${MSL_TAG}.tar.gz" \
 && wget -q -O buildings.tgz "https://github.com/lbl-srg/modelica-buildings/archive/refs/tags/${BUILDINGS_TAG}.tar.gz" \
 && tar xzf msl.tgz && tar xzf buildings.tgz \
 && mv ModelicaStandardLibrary-*/Modelica "/opt/omlib/Modelica 4.0.0" \
 && mv ModelicaStandardLibrary-*/ModelicaServices "/opt/omlib/ModelicaServices 4.0.0" \
 && mv ModelicaStandardLibrary-*/Complex.mo "/opt/omlib/Complex 4.0.0.mo" \
 && mv modelica-buildings-*/Buildings "/opt/omlib/Buildings 11.1.0" \
 && cd / && rm -rf /tmp/src
