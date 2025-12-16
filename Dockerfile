FROM devkitpro/devkitarm:latest

RUN apt-get update && apt-get install -y \
    cmake python3 python3-pip wget unzip sox \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for banner tools
RUN pip3 install --break-system-packages Pillow

WORKDIR /opt

# Install bannertool
RUN git clone https://github.com/Epicpkmn11/bannertool.git && \
    cd bannertool && \
    sed -i 's|git://github.com|https://github.com|g' .gitmodules && \
    git submodule update --init --recursive && \
    make && \
    cp output/linux-x86_64/bannertool /usr/local/bin/

# Install makerom
RUN wget https://github.com/3DSGuy/Project_CTR/releases/download/makerom-v0.18.4/makerom-v0.18.4-ubuntu_x86_64.zip && \
    unzip makerom-v0.18.4-ubuntu_x86_64.zip && \
    cp makerom /usr/local/bin/ && \
    chmod +x /usr/local/bin/makerom

# Install 3dstool (for banner extraction/building)
RUN wget https://github.com/dnasdw/3dstool/releases/download/v1.2.6/3dstool_linux_x86_64.tar.gz && \
    tar xzf 3dstool_linux_x86_64.tar.gz && \
    cp 3dstool /usr/local/bin/ && \
    chmod +x /usr/local/bin/3dstool && \
    rm 3dstool_linux_x86_64.tar.gz

# Clone forwarder project
RUN git clone https://github.com/HeyItsJono/mgba-3DS-Forwarder.git forwarder

# Copy banner tools
COPY banner_tools /opt/forwarder/banner_tools
RUN chmod +x /opt/forwarder/banner_tools/*.py

WORKDIR /opt/forwarder/mgba

# Patch source code for newer GCC
RUN sed -i 's/return 0;/return;/g' src/core/rewind.c src/core/thread.c src/feature/thread-proxy.c && \
    sed -i 's/uint32_t size/unsigned int size/' src/feature/updater.c

# Note: To replace the homebrew boot splash, place logo.darc.lz in the build context
# COPY logo.darc.lz /opt/forwarder/mgba/res/3ds/logo.darc.lz

# Build mGBA for 3DS
RUN mkdir -p build-3ds && cd build-3ds && \
    cmake -DCMAKE_TOOLCHAIN_FILE=/opt/forwarder/mgba/src/platform/3ds/CMakeToolchain.txt \
          -DCMAKE_INSTALL_PREFIX=/opt/forwarder/mgba/build-3ds/install \
          .. && \
    make -j$(nproc) && \
    make install

WORKDIR /work
COPY build_forwarder.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/build_forwarder.sh

ENTRYPOINT ["/usr/local/bin/build_forwarder.sh"]
