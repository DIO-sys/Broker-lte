/*
 * broker.cpp — Windowed Mixer + Gain + Noise Injection (v7)
 *
 * v7 = v6 with noise cap raised 10.0 → 2000.0. Measured calibration
 * (v5/v6 sweeps): signal amplitude A ≈ 3000, so
 *     SNR ≈ 20·log10(gain·3000 / sigma)
 * Link-degrading sigma therefore lives in the hundreds:
 *     sigma 30→~40dB, 300→~20dB, 950→~10dB, 1500→~6dB
 * The old 10.0 cap couldn't reach any of it.
 *
 * Compile:
 *   g++ -std=c++17 -O2 -o broker broker.cpp -lzmq -lpthread
 *
 * REQUIRES: srsRAN patched (rf_zmq_imp.c: goto clean_exit → num_tx_gap_samples = 0)
 * REQUIRES: REQ/REP configs (no tx_type/rx_type in device_args)
 *
 * v6 = v5 plus PER-UE GAUSSIAN NOISE INJECTION.
 *
 * Why (empirical, from the v5 gain sweep): srsRAN's ZMQ channel is
 * noiseless. Gain scaling moves RSRP exactly (20·log10(g), verified
 * 1.0→0.05 = −26dB) but SNR stays pinned (~141dB), MCS never moves,
 * BLER stays 0 — no noise floor means amplitude cannot degrade link
 * QUALITY, only measured power, until a hard decode cliff. So:
 *
 *   gain  = calibrated power lever (RSRP shift, observability, kill)
 *   noise = degradation primitive (SNR becomes a controlled quantity)
 *
 * Per UE, per direction: independent noise amplitude sigma (std dev
 * per complex component's magnitude scale — see table note below).
 * Applied at serve time only; queues stay clean; alignment invariant
 * untouched (noise adds, never shifts samples).
 *
 *   DL: ue_i's served chunk becomes  gain_i·x[n] + sigma_i·w[n]
 *   UL: ue_i's window contribution becomes gain_i·x[n] + sigma_i·w[n]
 *       (noise contributes even at gain=0 → "uplink contamination"
 *        fault: a UE slot emitting pure noise into the composite)
 *
 * Noise source: precomputed 1M-sample circular table of complex
 * Gaussian (unit average power per complex sample: each component
 * N(0, 1/√2)), consumed with a running offset. Statistically ample
 * for driving a decoder; avoids per-sample RNG cost on the hot path.
 *
 * Control (TCP 127.0.0.1:4000, JSON lines), v5 commands plus:
 *   {"cmd":"set_noise","ue":2,"dir":"dl","value":0.01}
 *   {"cmd":"reset"}      -> gains 1.0 AND noise 0.0
 *   {"cmd":"status"}     -> now includes dl_noise/ul_noise
 *
 * Calibration: signal sample amplitude is srsRAN-internal, so map
 * sigma→SNR empirically: sweep sigma at gain 1.0 and log dl_snr.
 * (Sweep script in the accompanying instructions.)
 */

#include <zmq.hpp>
#include <complex>
#include <vector>
#include <deque>
#include <chrono>
#include <cstring>
#include <cstdio>
#include <csignal>
#include <cmath>
#include <cerrno>
#include <cstdint>
#include <atomic>
#include <thread>
#include <string>
#include <sstream>
#include <random>

#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>

static volatile bool g_running = true;
static void sig_handler(int) { g_running = false; }

static const uint8_t REQ_BYTE   = 0xFF;
static const int     NUM_UES    = 3;
static const int     POLL_MS    = 1;
static const size_t  SAMP_SZ    = sizeof(std::complex<float>);

static const uint64_t WIN_SAMPS = 11520;
static const int      DEFER_MS  = 8;
static const int      LIVE_MS   = 1000;
static const uint64_t UL_MAX_SAMPS = 11520ull * 500;
static const size_t   DL_Q_MAX     = 256;
static const int      CTRL_PORT    = 4000;

static const size_t NOISE_TABLE_SAMPS = 1u << 20;   /* ~1M complex */

/* ── Instrumentation ── */
static const size_t RING_MAX         = 100;
static const int    POST_BURST_LOG   = 50;
static const float  BURST_ON_THRESH  = 1.0f;
static const float  BURST_OFF_THRESH = 0.01f;

using clk = std::chrono::steady_clock;

/* ── Runtime controls ── */
static std::atomic<float> g_dl_gain[NUM_UES];
static std::atomic<float> g_ul_gain[NUM_UES];
static std::atomic<float> g_dl_noise[NUM_UES];
static std::atomic<float> g_ul_noise[NUM_UES];

/* ── Noise table ── */
static std::vector<std::complex<float>> g_noise;
static size_t g_noise_off_dl[NUM_UES] = {0, 0, 0};
static size_t g_noise_off_ul[NUM_UES] = {0, 0, 0};

static void init_noise_table() {
    std::mt19937 rng(0xC0FFEE);
    std::normal_distribution<float> nd(0.f, 0.70710678f); /* 1/sqrt(2) */
    g_noise.resize(NOISE_TABLE_SAMPS);
    for (auto& v : g_noise) v = {nd(rng), nd(rng)};
    /* stagger per-UE start offsets so streams are uncorrelated */
    for (int i = 0; i < NUM_UES; i++) {
        g_noise_off_dl[i] = (i * 2 + 1) * (NOISE_TABLE_SAMPS / 7);
        g_noise_off_ul[i] = (i * 2 + 2) * (NOISE_TABLE_SAMPS / 7);
    }
}

static inline void add_noise(std::complex<float>* buf, size_t n,
                             float sigma, size_t& off) {
    if (sigma <= 0.f) return;
    for (size_t j = 0; j < n; j++) {
        buf[j] += g_noise[off] * sigma;
        off = (off + 1) & (NOISE_TABLE_SAMPS - 1);
    }
}

static void send_req(zmq::socket_t& sock) {
    zmq::message_t req(1);
    memcpy(req.data(), &REQ_BYTE, 1);
    sock.send(req);
}

struct UlEvent {
    uint64_t win_start;
    bool     timed_out;
    bool     deferred;
    float    energy[NUM_UES];
    uint64_t backlog[NUM_UES];
};

static void print_event(const UlEvent& ev) {
    printf("S=%-10lu %s%s ue1[e=%12.2f b=%lu] ue2[e=%12.2f b=%lu] ue3[e=%12.2f b=%lu]\n",
           (unsigned long)ev.win_start,
           ev.timed_out ? "T" : "F",
           ev.deferred ? "d" : " ",
           ev.energy[0], (unsigned long)ev.backlog[0],
           ev.energy[1], (unsigned long)ev.backlog[1],
           ev.energy[2], (unsigned long)ev.backlog[2]);
}

struct UlStream {
    std::deque<std::vector<uint8_t>> q;
    size_t   front_off    = 0;
    uint64_t abs_pos      = 0;
    uint64_t align_samp   = 0;
    uint64_t rx_samples   = 0;
    uint64_t late_dropped = 0;
    uint64_t ovf_dropped  = 0;
    bool     started      = false;
    bool     anchored     = false;
    clk::time_point last_rx;

    uint64_t end_abs() const { return align_samp + rx_samples; }
    uint64_t backlog() const { return end_abs() - abs_pos; }

    uint64_t drop_until(uint64_t upto) {
        uint64_t dropped = 0;
        while (abs_pos < upto && !q.empty()) {
            uint64_t have = (q.front().size() - front_off) / SAMP_SZ;
            uint64_t need = upto - abs_pos;
            uint64_t take = have < need ? have : need;
            front_off += take * SAMP_SZ;
            abs_pos   += take;
            dropped   += take;
            if (front_off >= q.front().size()) { q.pop_front(); front_off = 0; }
        }
        return dropped;
    }

    float mix_window(uint64_t S, uint64_t n, std::complex<float>* out,
                     float gain) {
        float energy = 0.f;
        late_dropped += drop_until(S);
        while (abs_pos < S + n && !q.empty()) {
            uint64_t have = (q.front().size() - front_off) / SAMP_SZ;
            if (have == 0) { q.pop_front(); front_off = 0; continue; }
            uint64_t off_in_win = abs_pos - S;
            uint64_t room = (S + n) - abs_pos;
            uint64_t take = have < room ? have : room;
            auto* s = reinterpret_cast<const std::complex<float>*>(
                q.front().data() + front_off);
            if (gain != 0.f) {
                for (uint64_t j = 0; j < take; j++) {
                    std::complex<float> v = s[j] * gain;
                    out[off_in_win + j] += v;
                    energy += std::norm(v);
                }
            }
            front_off += take * SAMP_SZ;
            abs_pos   += take;
            if (front_off >= q.front().size()) { q.pop_front(); front_off = 0; }
        }
        return energy;
    }
};

/* ── Control server ── */
static bool json_get_str(const std::string& s, const std::string& key,
                         std::string& out) {
    auto k = s.find("\"" + key + "\"");
    if (k == std::string::npos) return false;
    auto c = s.find(':', k);
    if (c == std::string::npos) return false;
    auto q1 = s.find('"', c + 1);
    if (q1 == std::string::npos) return false;
    auto q2 = s.find('"', q1 + 1);
    if (q2 == std::string::npos) return false;
    out = s.substr(q1 + 1, q2 - q1 - 1);
    return true;
}

static bool json_get_num(const std::string& s, const std::string& key,
                         double& out) {
    auto k = s.find("\"" + key + "\"");
    if (k == std::string::npos) return false;
    auto c = s.find(':', k);
    if (c == std::string::npos) return false;
    try { out = std::stod(s.substr(c + 1)); } catch (...) { return false; }
    return true;
}

static UlStream* g_ul_view = nullptr;

static std::string handle_ctrl_line(const std::string& line) {
    std::string cmd;
    if (!json_get_str(line, "cmd", cmd))
        return "{\"ok\":false,\"err\":\"no cmd\"}\n";

    if (cmd == "set_gain" || cmd == "set_noise") {
        double ue_d = 0, val = -1;
        std::string dir;
        if (!json_get_num(line, "ue", ue_d) ||
            !json_get_str(line, "dir", dir) ||
            !json_get_num(line, "value", val))
            return "{\"ok\":false,\"err\":\"need ue, dir, value\"}\n";
        int ue = (int)ue_d - 1;
        if (ue < 0 || ue >= NUM_UES)
            return "{\"ok\":false,\"err\":\"ue out of range\"}\n";
        bool is_gain = (cmd == "set_gain");
        if (is_gain && (val < 0.0 || val > 1.0))
            return "{\"ok\":false,\"err\":\"gain must be 0.0-1.0\"}\n";
        if (!is_gain && (val < 0.0 || val > 2000.0))
            return "{\"ok\":false,\"err\":\"noise must be 0.0-2000.0\"}\n";
        std::atomic<float>* target = nullptr;
        if (dir == "dl") target = is_gain ? &g_dl_gain[ue] : &g_dl_noise[ue];
        else if (dir == "ul") target = is_gain ? &g_ul_gain[ue] : &g_ul_noise[ue];
        else return "{\"ok\":false,\"err\":\"dir must be dl|ul\"}\n";
        target->store((float)val);
        printf("[ctrl] ue%d %s %s -> %.4f\n", ue + 1, dir.c_str(),
               is_gain ? "gain" : "noise", val);
        return "{\"ok\":true}\n";
    }

    if (cmd == "kill") {
        double ue_d = 0;
        if (!json_get_num(line, "ue", ue_d))
            return "{\"ok\":false,\"err\":\"need ue\"}\n";
        int ue = (int)ue_d - 1;
        if (ue < 0 || ue >= NUM_UES)
            return "{\"ok\":false,\"err\":\"ue out of range\"}\n";
        g_dl_gain[ue].store(0.f);
        g_ul_gain[ue].store(0.f);
        printf("[ctrl] ue%d SOFT-KILLED (dl=ul gain=0.0)\n", ue + 1);
        return "{\"ok\":true,\"note\":\"soft kill: dl=ul gain 0.0\"}\n";
    }

    if (cmd == "reset") {
        for (int i = 0; i < NUM_UES; i++) {
            g_dl_gain[i].store(1.f);
            g_ul_gain[i].store(1.f);
            g_dl_noise[i].store(0.f);
            g_ul_noise[i].store(0.f);
        }
        printf("[ctrl] all gains -> 1.0, all noise -> 0.0\n");
        return "{\"ok\":true}\n";
    }

    if (cmd == "status") {
        std::ostringstream os;
        os << "{\"ok\":true,\"ues\":[";
        for (int i = 0; i < NUM_UES; i++) {
            os << (i ? "," : "") << "{\"ue\":" << (i + 1)
               << ",\"dl_gain\":" << g_dl_gain[i].load()
               << ",\"ul_gain\":" << g_ul_gain[i].load()
               << ",\"dl_noise\":" << g_dl_noise[i].load()
               << ",\"ul_noise\":" << g_ul_noise[i].load();
            if (g_ul_view) {
                os << ",\"started\":" << (g_ul_view[i].started ? "true" : "false")
                   << ",\"backlog\":" << (g_ul_view[i].started
                                          ? g_ul_view[i].backlog() : 0)
                   << ",\"late_dropped\":" << g_ul_view[i].late_dropped;
            }
            os << "}";
        }
        os << "]}\n";
        return os.str();
    }

    return "{\"ok\":false,\"err\":\"unknown cmd\"}\n";
}

static void ctrl_thread_fn() {
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { perror("[ctrl] socket"); return; }
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(CTRL_PORT);
    if (bind(srv, (sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("[ctrl] bind"); close(srv); return;
    }
    listen(srv, 4);
    printf("[ctrl] listening on 127.0.0.1:%d\n", CTRL_PORT);

    while (g_running) {
        fd_set fds; FD_ZERO(&fds); FD_SET(srv, &fds);
        timeval tv{0, 200000};
        if (select(srv + 1, &fds, nullptr, nullptr, &tv) <= 0) continue;
        int cli = accept(srv, nullptr, nullptr);
        if (cli < 0) continue;
        std::string buf;
        char tmp[512];
        while (g_running) {
            ssize_t n = recv(cli, tmp, sizeof(tmp), 0);
            if (n <= 0) break;
            buf.append(tmp, n);
            size_t nl;
            while ((nl = buf.find('\n')) != std::string::npos) {
                std::string line = buf.substr(0, nl);
                buf.erase(0, nl + 1);
                if (line.empty()) continue;
                std::string reply = handle_ctrl_line(line);
                send(cli, reply.data(), reply.size(), 0);
            }
        }
        close(cli);
    }
    close(srv);
}

int main() {
    std::signal(SIGINT, sig_handler);
    std::signal(SIGTERM, sig_handler);
    std::signal(SIGPIPE, SIG_IGN);

    for (int i = 0; i < NUM_UES; i++) {
        g_dl_gain[i].store(1.f);
        g_ul_gain[i].store(1.f);
        g_dl_noise[i].store(0.f);
        g_ul_noise[i].store(0.f);
    }
    init_noise_table();

    printf("================================================================\n");
    printf("  C++ ZMQ Broker v6 — mixer + gain + noise injection\n");
    printf("  Control: TCP 127.0.0.1:%d, JSON lines\n", CTRL_PORT);
    printf("    set_gain / set_noise {ue, dir, value} | kill | reset | status\n");
    printf("  eNB(3101/3100) <-> UE1(3000/3001) UE2(3010/3011) UE3(3020/3021)\n");
    printf("================================================================\n");

    zmq::context_t ctx(1);
    int linger = 0;

    zmq::socket_t enb_dl(ctx, ZMQ_REQ);
    enb_dl.setsockopt(ZMQ_LINGER, &linger, sizeof(linger));
    enb_dl.connect("tcp://localhost:3101");

    zmq::socket_t enb_ul(ctx, ZMQ_REP);
    enb_ul.setsockopt(ZMQ_LINGER, &linger, sizeof(linger));
    enb_ul.bind("tcp://*:3100");

    const char* ue_dl_addrs[] = {"tcp://*:3000", "tcp://*:3010", "tcp://*:3020"};
    zmq::socket_t ue_dl[NUM_UES] = {
        zmq::socket_t(ctx, ZMQ_REP),
        zmq::socket_t(ctx, ZMQ_REP),
        zmq::socket_t(ctx, ZMQ_REP)
    };
    for (int i = 0; i < NUM_UES; i++) {
        ue_dl[i].setsockopt(ZMQ_LINGER, &linger, sizeof(linger));
        ue_dl[i].bind(ue_dl_addrs[i]);
    }

    const char* ue_ul_addrs[] = {
        "tcp://localhost:3001", "tcp://localhost:3011", "tcp://localhost:3021"
    };
    zmq::socket_t ue_ul[NUM_UES] = {
        zmq::socket_t(ctx, ZMQ_REQ),
        zmq::socket_t(ctx, ZMQ_REQ),
        zmq::socket_t(ctx, ZMQ_REQ)
    };
    for (int i = 0; i < NUM_UES; i++) {
        ue_ul[i].setsockopt(ZMQ_LINGER, &linger, sizeof(linger));
        ue_ul[i].connect(ue_ul_addrs[i]);
        send_req(ue_ul[i]);
    }

    send_req(enb_dl);

    std::deque<std::vector<uint8_t>> dl_q;
    std::deque<uint64_t> dl_q_start;
    uint64_t dl_base = 0;
    uint64_t dl_next = 0;
    uint64_t dl_total_samps = 0;
    uint64_t ue_dl_cursor[NUM_UES] = {0, 0, 0};
    bool     ue_dl_pending[NUM_UES] = {false, false, false};
    bool     ue_dl_seen[NUM_UES]    = {false, false, false};
    uint64_t dl_dropped = 0;

    static UlStream ul[NUM_UES];
    g_ul_view = ul;

    uint64_t win_start = 0;
    bool            enb_ul_waiting = false;
    clk::time_point enb_ul_deadline;

    size_t   chunk_bytes = 0;
    uint64_t dl_n = 0, serve_n = 0;
    uint64_t serves_full = 0, serves_timeout = 0, serves_deferred_ok = 0;

    std::deque<UlEvent> ul_ring;
    bool burst_active = false;
    int  post_burst_left = 0;
    int  burst_count = 0;

    std::thread ctrl_thread(ctrl_thread_fn);

    zmq::pollitem_t items[5];
    items[0] = {static_cast<void*>(enb_dl), 0, ZMQ_POLLIN, 0};
    items[1] = {static_cast<void*>(enb_ul), 0, ZMQ_POLLIN, 0};
    for (int i = 0; i < NUM_UES; i++)
        items[2 + i] = {static_cast<void*>(ue_ul[i]), 0, ZMQ_POLLIN, 0};

    auto ue_is_live = [&](int i) {
        return ul[i].started &&
               (clk::now() - ul[i].last_rx) < std::chrono::milliseconds(LIVE_MS);
    };

    auto window_ready = [&]() {
        uint64_t win_end = win_start + WIN_SAMPS;
        for (int i = 0; i < NUM_UES; i++) {
            if (!ue_is_live(i)) continue;
            if (ul[i].align_samp >= win_end) continue;
            if (ul[i].end_abs() < win_end) return false;
        }
        return true;
    };

    auto serve_window = [&](bool timed_out) {
        UlEvent ev{};
        ev.win_start = win_start;
        ev.timed_out = timed_out;
        ev.deferred  = enb_ul_waiting;

        std::vector<std::complex<float>> combined(WIN_SAMPS, {0.f, 0.f});
        for (int i = 0; i < NUM_UES; i++) {
            if (ul[i].started)
                ev.energy[i] = ul[i].mix_window(win_start, WIN_SAMPS,
                                                combined.data(),
                                                g_ul_gain[i].load());
            /* UL noise: contributes regardless of data/gain — this IS
             * the uplink-contamination fault when gain=0/noise>0. */
            add_noise(combined.data(), WIN_SAMPS,
                      g_ul_noise[i].load(), g_noise_off_ul[i]);
            ev.backlog[i] = ul[i].started ? ul[i].backlog() : 0;
        }

        zmq::message_t reply(WIN_SAMPS * SAMP_SZ);
        memcpy(reply.data(), combined.data(), WIN_SAMPS * SAMP_SZ);
        enb_ul.send(reply);
        enb_ul_waiting = false;

        if (timed_out) serves_timeout++;
        else { serves_full++; if (ev.deferred) serves_deferred_ok++; }

        win_start += WIN_SAMPS;

        ul_ring.push_back(ev);
        if (ul_ring.size() > RING_MAX) ul_ring.pop_front();
        if (post_burst_left > 0) {
            print_event(ev);
            if (--post_burst_left == 0)
                printf("===== end of post-burst window #%d =====\n\n",
                       burst_count);
        }
        float e_now = ev.energy[0];
        if (!burst_active && e_now > BURST_ON_THRESH) {
            burst_active = true;
            burst_count++;
            printf("\n===== UL BURST #%d at S=%lu (ue1 e=%.2f) =====\n",
                   burst_count, (unsigned long)ev.win_start, e_now);
            for (const auto& past : ul_ring) print_event(past);
            printf("===== live-logging next %d serves =====\n", POST_BURST_LOG);
            post_burst_left = POST_BURST_LOG;
        } else if (burst_active && e_now < BURST_OFF_THRESH) {
            burst_active = false;
        }

        serve_n++;
        if (serve_n % 1000 == 0)
            printf("[ul] %lu windows (full: %lu, timeout: %lu) "
                   "backlog=[%lu,%lu,%lu] late=[%lu,%lu,%lu]\n",
                   (unsigned long)serve_n, (unsigned long)serves_full,
                   (unsigned long)serves_timeout,
                   (unsigned long)(ul[0].started ? ul[0].backlog() : 0),
                   (unsigned long)(ul[1].started ? ul[1].backlog() : 0),
                   (unsigned long)(ul[2].started ? ul[2].backlog() : 0),
                   (unsigned long)ul[0].late_dropped,
                   (unsigned long)ul[1].late_dropped,
                   (unsigned long)ul[2].late_dropped);
    };

    auto serve_ue_dl = [&](int i) {
        size_t idx = (size_t)(ue_dl_cursor[i] - dl_base);
        auto& chunk = dl_q[idx];
        if (!ue_dl_seen[i] || !ul[i].anchored) {
            ul[i].align_samp = dl_q_start[idx];
            ul[i].abs_pos    = dl_q_start[idx] + ul[i].rx_samples;
            ul[i].anchored   = true;
            printf("[align] ue%d anchored: UL stream starts at abs sample %lu\n",
                   i + 1, (unsigned long)ul[i].align_samp);
        }
        float gain  = g_dl_gain[i].load();
        float sigma = g_dl_noise[i].load();
        size_t n = chunk.size() / SAMP_SZ;
        zmq::message_t reply(chunk.size());
        auto* src = reinterpret_cast<const std::complex<float>*>(chunk.data());
        auto* dst = reinterpret_cast<std::complex<float>*>(reply.data());
        if (gain == 1.f && sigma <= 0.f) {
            memcpy(reply.data(), chunk.data(), chunk.size());
        } else {
            for (size_t j = 0; j < n; j++) dst[j] = src[j] * gain;
            add_noise(dst, n, sigma, g_noise_off_dl[i]);
        }
        ue_dl[i].send(reply);
        ue_dl_cursor[i]++;
        ue_dl_pending[i] = false;
    };

    auto trim_dl = [&]() {
        uint64_t min_cursor = UINT64_MAX;
        bool anyone = false;
        for (int i = 0; i < NUM_UES; i++)
            if (ue_dl_seen[i]) { anyone = true;
                if (ue_dl_cursor[i] < min_cursor) min_cursor = ue_dl_cursor[i]; }
        if (!anyone) min_cursor = dl_next;
        while (!dl_q.empty() && dl_base < min_cursor) {
            dl_q.pop_front();
            dl_q_start.pop_front();
            dl_base++;
        }
    };

    printf("Running. Ctrl+C to stop.\n\n");

    try {
        while (g_running) {
            for (int i = 0; i < 5; i++) items[i].revents = 0;
            zmq::poll(items, 5, POLL_MS);

            for (int i = 0; i < NUM_UES; i++) {
                if (items[2 + i].revents & ZMQ_POLLIN) {
                    zmq::message_t msg;
                    ue_ul[i].recv(&msg);
                    uint64_t n_samps = msg.size() / SAMP_SZ;
                    if (!ul[i].started) {
                        ul[i].started = true;
                        printf("[ul] ue%d first UL data (%lu samples)%s\n",
                               i + 1, (unsigned long)n_samps,
                               ul[i].anchored ? "" : " [pre-anchor!]");
                    }
                    ul[i].q.emplace_back((uint8_t*)msg.data(),
                                         (uint8_t*)msg.data() + msg.size());
                    ul[i].rx_samples += n_samps;
                    ul[i].last_rx = clk::now();
                    if (ul[i].backlog() > UL_MAX_SAMPS) {
                        uint64_t d = ul[i].drop_until(ul[i].end_abs()
                                                      - UL_MAX_SAMPS);
                        ul[i].ovf_dropped += d;
                        printf("[warn] ue%d UL backlog cap, dropped %lu\n",
                               i + 1, (unsigned long)d);
                    }
                    send_req(ue_ul[i]);
                }
            }

            if (items[1].revents & ZMQ_POLLIN) {
                zmq::message_t req;
                enb_ul.recv(&req);
                enb_ul_waiting = true;
                enb_ul_deadline = clk::now() +
                                  std::chrono::milliseconds(DEFER_MS);
            }
            if (enb_ul_waiting) {
                if (window_ready())
                    serve_window(false);
                else if (clk::now() >= enb_ul_deadline)
                    serve_window(true);
            }

            if (items[0].revents & ZMQ_POLLIN) {
                zmq::message_t msg;
                enb_dl.recv(&msg);
                dl_q.emplace_back((uint8_t*)msg.data(),
                                  (uint8_t*)msg.data() + msg.size());
                dl_q_start.push_back(dl_total_samps);
                dl_total_samps += msg.size() / SAMP_SZ;
                dl_next++;
                if (chunk_bytes == 0) {
                    chunk_bytes = msg.size();
                    printf("[dl] First chunk: %zu bytes (%zu samples)\n",
                           chunk_bytes, chunk_bytes / SAMP_SZ);
                }
                send_req(enb_dl);

                dl_n++;
                if (dl_n % 1000 == 0)
                    printf("[dl] %lu chunks (q depth %zu, dropped %lu)\n",
                           (unsigned long)dl_n, dl_q.size(),
                           (unsigned long)dl_dropped);

                if (dl_q.size() > DL_Q_MAX) {
                    for (int i = 0; i < NUM_UES; i++)
                        if (ue_dl_seen[i] && ue_dl_cursor[i] == dl_base)
                            ue_dl_cursor[i]++;
                    dl_q.pop_front();
                    dl_q_start.pop_front();
                    dl_base++;
                    dl_dropped++;
                }

                for (int i = 0; i < NUM_UES; i++)
                    if (ue_dl_pending[i] && ue_dl_cursor[i] < dl_next)
                        serve_ue_dl(i);
                trim_dl();
            }

            for (int i = 0; i < NUM_UES; i++) {
                if (ue_dl_pending[i]) continue;
                zmq::pollitem_t it = {static_cast<void*>(ue_dl[i]), 0,
                                      ZMQ_POLLIN, 0};
                zmq::poll(&it, 1, 0);
                if (it.revents & ZMQ_POLLIN) {
                    zmq::message_t req;
                    ue_dl[i].recv(&req);
                    if (!ue_dl_seen[i]) {
                        ue_dl_seen[i] = true;
                        ue_dl_cursor[i] = dl_next > 0 ? dl_next - 1 : 0;
                        if (ue_dl_cursor[i] < dl_base) ue_dl_cursor[i] = dl_base;
                    }
                    if (ue_dl_cursor[i] < dl_next)
                        serve_ue_dl(i);
                    else
                        ue_dl_pending[i] = true;
                }
                trim_dl();
            }
        }
    } catch (const zmq::error_t& e) {
        if (e.num() != EINTR)
            fprintf(stderr, "[fatal] zmq error: %s\n", e.what());
    }

    g_running = false;
    ctrl_thread.join();

    printf("\n========== Final Stats ==========\n");
    printf("DL chunks:   %lu (dropped: %lu)\n",
           (unsigned long)dl_n, (unsigned long)dl_dropped);
    printf("UL windows:  %lu (full: %lu, timeout: %lu)\n",
           (unsigned long)serve_n, (unsigned long)serves_full,
           (unsigned long)serves_timeout);
    for (int i = 0; i < NUM_UES; i++)
        printf("ue%d: anchor=%lu rx=%lu late=%lu ovf=%lu "
               "g(dl/ul)=%.2f/%.2f n(dl/ul)=%.4f/%.4f\n",
               i + 1, (unsigned long)ul[i].align_samp,
               (unsigned long)ul[i].rx_samples,
               (unsigned long)ul[i].late_dropped,
               (unsigned long)ul[i].ovf_dropped,
               g_dl_gain[i].load(), g_ul_gain[i].load(),
               g_dl_noise[i].load(), g_ul_noise[i].load());
    printf("=================================\n");

    enb_dl.close();
    enb_ul.close();
    for (int i = 0; i < NUM_UES; i++) { ue_dl[i].close(); ue_ul[i].close(); }
    ctx.close();
    printf("Done.\n");
    return 0;
}