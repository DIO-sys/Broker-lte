#!/bin/bash
###############################################################################
# srsRAN 4G + ZMQ + GNU Radio — Full Installation Script
#
# Follows:
#   - https://docs.srsran.com/projects/4g/en/rfsoc/general/source/1_installation.html
#   - https://docs.srsran.com/projects/4g/en/rfsoc/app_notes/source/zeromq/source/index.html
#
# Run as your normal user (script uses sudo where needed).
# Tested on Ubuntu 20.04, 22.04, 24.04.
###############################################################################

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INSTALL]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

WORK_DIR="${HOME}/srsran-build"
mkdir -p "${WORK_DIR}"

###############################################################################
# 1. System dependencies (from srsRAN install guide)
###############################################################################
log "Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y \
    build-essential \
    cmake \
    git \
    libfftw3-dev \
    libmbedtls-dev \
    libboost-program-options-dev \
    libconfig++-dev \
    libsctp-dev \
    libpcsclite-dev \
    libdw-dev \
    libtool \
    autoconf \
    automake \
    pkg-config \
    net-tools \
    iperf3 \
    tcpdump

###############################################################################
# 2. ZeroMQ — install from source (per ZMQ app note)
###############################################################################
log "Installing libzmq from source..."
cd "${WORK_DIR}"
if [ ! -d libzmq ]; then
    git clone https://github.com/zeromq/libzmq.git
fi
cd libzmq
./autogen.sh
./configure
make -j"$(nproc)"
sudo make install
sudo ldconfig

log "Installing czmq from source..."
cd "${WORK_DIR}"
if [ ! -d czmq ]; then
    git clone https://github.com/zeromq/czmq.git
fi
cd czmq
./autogen.sh
./configure
make -j"$(nproc)"
sudo make install
sudo ldconfig

###############################################################################
# 3. GNU Radio (needed for the multi-UE GRC broker)
###############################################################################
log "Installing GNU Radio..."
sudo apt-get install -y gnuradio

# Verify ZMQ blocks are available
if python3 -c "from gnuradio import zeromq" 2>/dev/null; then
    log "GNU Radio ZMQ blocks confirmed available."
else
    warn "GNU Radio ZMQ blocks not found, installing gr-zeromq..."
    sudo apt-get install -y gr-zeromq || warn "gr-zeromq package not found — ZMQ blocks may be bundled with your gnuradio version."
fi

###############################################################################
# 4. srsRAN 4G — build from source with ZMQ support
###############################################################################
log "Cloning and building srsRAN 4G..."
cd "${WORK_DIR}"
if [ ! -d srsRAN_4G ]; then
    git clone https://github.com/srsRAN/srsRAN_4G.git
fi
cd srsRAN_4G
mkdir -p build
cd build

# Clean build to ensure ZMQ is picked up
rm -f CMakeCache.txt
cmake ../
# Check that ZMQ was found
if grep -q "ZEROMQ" CMakeCache.txt 2>/dev/null || cmake ../ 2>&1 | grep -qi "zeromq"; then
    log "CMake found ZeroMQ — good."
else
    warn "Could not confirm ZMQ detection in cmake output."
    warn "Check the cmake output above for: 'Found libZEROMQ: /usr/local/include, /usr/local/lib/libzmq.so'"
fi

make -j"$(nproc)"
sudo make install

# Install default configs to ~/.config/srsran (user-level)
sudo srsran_install_configs.sh user
log "Default srsRAN configs installed to ~/.config/srsran/"

###############################################################################
# 5. Verify installations
###############################################################################
log "============================================"
log "Verifying installations..."
log "============================================"

PASS=true

if command -v srsenb &>/dev/null; then
    log "srsenb ............. $(which srsenb)"
else
    err "srsenb not found in PATH"
    PASS=false
fi

if command -v srsue &>/dev/null; then
    log "srsue .............. $(which srsue)"
else
    err "srsue not found in PATH"
    PASS=false
fi

if command -v srsepc &>/dev/null; then
    log "srsepc ............. $(which srsepc)"
else
    err "srsepc not found in PATH"
    PASS=false
fi

if command -v gnuradio-companion &>/dev/null; then
    log "gnuradio-companion . $(which gnuradio-companion)"
else
    warn "gnuradio-companion not in PATH (headless is fine — we use Python)"
fi

if python3 -c "from gnuradio import gr; print(gr.version())" 2>/dev/null; then
    GR_VER=$(python3 -c "from gnuradio import gr; print(gr.version())")
    log "GNU Radio version .. ${GR_VER}"
else
    err "GNU Radio Python bindings not working"
    PASS=false
fi

if ldconfig -p | grep -q libzmq; then
    log "libzmq ............. found"
else
    err "libzmq not found in ldconfig"
    PASS=false
fi

if ldconfig -p | grep -q libczmq; then
    log "libczmq ............ found"
else
    err "libczmq not found in ldconfig"
    PASS=false
fi

echo ""
if [ "$PASS" = true ]; then
    log "============================================"
    log "ALL DEPENDENCIES INSTALLED SUCCESSFULLY"
    log "============================================"
    log ""
    log "Next steps:"
    log "  1. Copy your config files into place"
    log "  2. Run the startup script"
else
    err "Some installations failed — check output above."
fi