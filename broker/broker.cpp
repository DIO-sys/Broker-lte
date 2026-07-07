/*
 * broker.cpp — Aligned Lossless-FIFO REQ/REP Broker for srsRAN 4G  (v3)
 *
 * Compile:
 *   g++ -std=c++17 -O2 -o broker broker.cpp -lzmq -lpthread
 *
 * REQUIRES: srsRAN patched (rf_zmq_imp.c: goto clean_exit → num_tx_gap_samples = 0)
 * REQUIRES: REQ/REP configs (no tx_type/rx_type in device_args)
 *
 * ─────────────────────────────────────────────────────────────────
 * v3: DETERMINISTIC SAMPLE-TIMELINE ALIGNMENT
 * ─────────────────────────────────────────────────────────────────
 * v2 made both directions lossless FIFOs (every chunk, once, in
 * order). Result: SIB2 decode became deterministic and the UE fired
 * 10 clean PRACH attempts, every one delivered fresh and in-order —
 * but the eNB never answered with a RAR.
 *
 * Why: srsRAN's ZMQ driver starts TX and RX sample counters at zero
 * AT DRIVER OPEN, on both ends. The UE self-aligns its TX stream to
 * its RX stream (its own tx_align); it places PRACH at a TX sample
 * index aligned to the DL subframe timing it decoded. The eNB
 * searches PRACH only at configured occasions in ITS OWN UL sample
 * timeline. Detection therefore requires:
 *
 *     UE_i TX chunk j  →  eNB UL serve (ul_align_i + j)
 *     where ul_align_i = absolute DL chunk seq of the FIRST chunk
 *                        the broker served to UE_i
 *
 * (Both counters tick 1 chunk = 11520 samples = 1 subframe.)
 *
 * v1/v2 never enforced this — UE chunk 0 landed on whatever serve
 * the eNB happened to be on. PRACH occasions repeat every 10ms, so
 * detection was luck modulo 10: explains v1-run1 detecting
 * preambles, v1-run2 not RACHing usefully, v2 sending 10 preambles
 * into the void.
 *
 * v3 rules:
 *   A. Record ul_align[i] when UE_i receives its first DL chunk.
 *   B. Tag every UL chunk from UE_i with its arrival index j.
 *      Serve number m includes UE_i's chunk iff j == m - ul_align[i].
 *   C. If UE_i's due chunk hasn't arrived, HOLD the eNB's reply
 *      (REP permits this; the eNB's async RX thread just blocks,
 *      same as against GRC). NEVER zero-insert while a UE is active:
 *      one insertion shifts the mapping by a subframe forever.
 *   D. Before any UE is active, timeout zero-fill (DEFER_MS) keeps
 *      the eNB clocked, exactly as v2.
 *   E. If an active UE stalls > UE_DEAD_MS, mark it inactive (its
 *      slot contributes zeros; alignment of others is unaffected).
 *
 * DL side unchanged from v2: shared FIFO + per-UE cursor, every
 * chunk exactly once, in order, held REQ when at live edge.
 *
 * Protocol: 0xFF REQ (1 byte), IQ samples REP.
 * ─────────────────────────────────────────────────────────────────
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

static volatile bool g_running = true;
static void sig_handler(int) { g_running = false; }

static const uint8_t REQ_BYTE = 0xFF;
static const int NUM_UES      = 3;
static const int POLL_MS      = 1;
static const size_t SAMP_SZ   = sizeof(std::complex<float>);

static const size_t DL_Q_MAX  = 256;
static const size_t UL_Q_MAX  = 256;

/* Zero-fill timeout — applies ONLY while no UE is active. */
static const int DEFER_MS     = 5;
/* An active UE silent this long is declared dead (stops being waited on). */
static const int UE_DEAD_MS   = 200;

/* ── Instrumentation ── */
static const size_t RING_MAX         = 100;
static const int    POST_BURST_LOG   = 50;
static const float  BURST_ON_THRESH  = 1.0f;
static const float  BURST_OFF_THRESH = 0.01f;

using clk = std::chrono::steady_clock;

static void send_req(zmq::socket_t& sock) {
    zmq::message_t req(1);
    memcpy(req.data(), &REQ_BYTE, 1);
    sock.send(req);
}

static float buf_energy(const std::vector<uint8_t>& buf) {
    size_t n = buf.size() / SAMP_SZ;
    if (n == 0) return 0.f;
    auto* s = reinterpret_cast<const std::complex<float>*>(buf.data());
    float e = 0.f;
    for (size_t j = 0; j < n; j++) e += std::norm(s[j]);
    return e;
}

struct UlChunk {
    uint64_t idx;                    /* arrival index j for this UE */
    std::vector<uint8_t> data;
};

struct UlEvent {
    uint64_t serve_n;
    char     kind;                   /* 'F' aligned data, 'Z' pre-UE zero */
    bool     deferred;
    float    energy[NUM_UES];
    long     lag[NUM_UES];           /* due_j - front_j (0 = perfect) */
};

static void print_event(const UlEvent& ev) {
    printf("s=%-8lu %c%s ue1[e=%12.2f l=%ld] ue2[e=%12.2f l=%ld] ue3[e=%12.2f l=%ld]\n",
           (unsigned long)ev.serve_n, ev.kind, ev.deferred ? "d" : " ",
           ev.energy[0], ev.lag[0],
           ev.energy[1], ev.lag[1],
           ev.energy[2], ev.lag[2]);
}

int main() {
    std::signal(SIGINT, sig_handler);
    std::signal(SIGTERM, sig_handler);

    printf("================================================================\n");
    printf("  C++ ZMQ Broker v3 — aligned lossless FIFO\n");
    printf("  UL rule: UE_i chunk j -> eNB serve (ul_align_i + j)\n");
    printf("  No zero-insertion while any UE is active (holds instead)\n");
    printf("  eNB(3101/3100) <-> UE1(3000/3001) UE2(3010/3011) UE3(3020/3021)\n");
    printf("  POLL_MS=%d, DEFER_MS=%d (pre-UE only), UE_DEAD_MS=%d\n",
           POLL_MS, DEFER_MS, UE_DEAD_MS);
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

    /* ── DL: shared FIFO + per-UE cursors (unchanged from v2) ── */
    std::deque<std::vector<uint8_t>> dl_q;
    uint64_t dl_base = 0;
    uint64_t dl_next = 0;
    uint64_t ue_dl_cursor[NUM_UES] = {0, 0, 0};
    bool     ue_dl_pending[NUM_UES] = {false, false, false};
    bool     ue_dl_seen[NUM_UES]    = {false, false, false};
    uint64_t dl_dropped = 0;

    /* ── UL: per-UE indexed FIFO + alignment state ── */
    std::deque<UlChunk> ul_q[NUM_UES];
    uint64_t ul_rx_count[NUM_UES] = {0, 0, 0};  /* arrival index counter j   */
    uint64_t ul_align[NUM_UES]    = {0, 0, 0};  /* DL seq of UE's 1st chunk  */
    bool     ue_active[NUM_UES]   = {false, false, false};
    bool     ue_dead[NUM_UES]     = {false, false, false};
    clk::time_point ue_last_ul[NUM_UES];
    uint64_t ul_dropped[NUM_UES]  = {0, 0, 0};

    /* eNB UL serve counter — m. Serve m carries UE_i chunk (m - ul_align_i). */
    uint64_t ul_serve_n = 0;

    bool            enb_ul_waiting = false;
    clk::time_point enb_ul_deadline;

    size_t chunk_bytes = 0;

    uint64_t dl_n = 0;
    uint64_t ul_fresh = 0, ul_zero = 0, ul_deferred_ok = 0;

    std::deque<UlEvent> ul_ring;
    bool burst_active = false;
    int  post_burst_left = 0;
    int  burst_count = 0;

    zmq::pollitem_t items[5];
    items[0] = {static_cast<void*>(enb_dl), 0, ZMQ_POLLIN, 0};
    items[1] = {static_cast<void*>(enb_ul), 0, ZMQ_POLLIN, 0};
    for (int i = 0; i < NUM_UES; i++)
        items[2 + i] = {static_cast<void*>(ue_ul[i]), 0, ZMQ_POLLIN, 0};

    auto any_ue_live = [&]() {
        for (int i = 0; i < NUM_UES; i++)
            if (ue_active[i] && !ue_dead[i]) return true;
        return false;
    };

    /* Can serve m be assembled now?
     * Ready iff every live UE whose stream covers serve m has its due
     * chunk at the queue front. UEs whose stream hasn't started yet
     * (m < ul_align) or that are dead/inactive contribute zeros. */
    auto ul_ready = [&](uint64_t m) {
        for (int i = 0; i < NUM_UES; i++) {
            if (!ue_active[i] || ue_dead[i]) continue;
            if (m < ul_align[i]) continue;          /* before UE joined */
            uint64_t due = m - ul_align[i];
            if (ul_q[i].empty()) return false;      /* due chunk not here yet */
            /* Discard anything older than due (shouldn't exist) */
            while (!ul_q[i].empty() && ul_q[i].front().idx < due) {
                ul_q[i].pop_front();
                if (++ul_dropped[i] % 50 == 1)
                    printf("[warn] ue%d UL chunk behind schedule, dropped "
                           "(total %lu)\n", i + 1, (unsigned long)ul_dropped[i]);
            }
            if (ul_q[i].empty() || ul_q[i].front().idx != due) return false;
        }
        return true;
    };

    /* Assemble and send serve m. kind: 'F' if any live UE contributed
     * (or could), 'Z' for pre-UE zero-fill. */
    auto serve_enb_ul = [&](char kind) {
        UlEvent ev{};
        ev.serve_n  = ul_serve_n;
        ev.deferred = enb_ul_waiting;
        ev.kind     = kind;

        size_t len = chunk_bytes ? chunk_bytes : 11520 * SAMP_SZ;
        size_t n_samples = len / SAMP_SZ;
        std::vector<std::complex<float>> combined(n_samples, {0.f, 0.f});

        if (kind == 'F') {
            for (int i = 0; i < NUM_UES; i++) {
                ev.lag[i] = -1;  /* not contributing */
                if (!ue_active[i] || ue_dead[i]) continue;
                if (ul_serve_n < ul_align[i]) continue;
                uint64_t due = ul_serve_n - ul_align[i];
                if (ul_q[i].empty() || ul_q[i].front().idx != due) continue;

                auto& chunk = ul_q[i].front();
                ev.energy[i] = buf_energy(chunk.data);
                ev.lag[i] = 0;
                if (chunk.data.size() != len && chunk.data.size() != 0) {
                    /* Size mismatch would break chunk<->subframe arithmetic */
                    static bool warned = false;
                    if (!warned) {
                        printf("[warn] ue%d chunk size %zu != %zu — "
                               "alignment arithmetic assumes constant size!\n",
                               i + 1, chunk.data.size(), len);
                        warned = true;
                    }
                }
                size_t n = chunk.data.size() / SAMP_SZ;
                auto* s = reinterpret_cast<const std::complex<float>*>(chunk.data.data());
                for (size_t j = 0; j < n && j < n_samples; j++)
                    combined[j] += s[j];
                ul_q[i].pop_front();
            }
            ul_fresh++;
            if (ev.deferred) ul_deferred_ok++;
        } else {
            ul_zero++;
        }

        zmq::message_t reply(len);
        memcpy(reply.data(), combined.data(), len);
        enb_ul.send(reply);
        enb_ul_waiting = false;

        ul_ring.push_back(ev);
        if (ul_ring.size() > RING_MAX) ul_ring.pop_front();

        if (post_burst_left > 0) {
            print_event(ev);
            if (--post_burst_left == 0)
                printf("===== end of post-burst window #%d =====\n\n", burst_count);
        }
        float e_now = ev.energy[0];
        if (!burst_active && e_now > BURST_ON_THRESH) {
            burst_active = true;
            burst_count++;
            printf("\n===== UL BURST #%d at serve %lu (ue1 e=%.2f) =====\n",
                   burst_count, (unsigned long)ul_serve_n, e_now);
            for (const auto& past : ul_ring) print_event(past);
            printf("===== live-logging next %d serves =====\n", POST_BURST_LOG);
            post_burst_left = POST_BURST_LOG;
        } else if (burst_active && e_now < BURST_OFF_THRESH) {
            burst_active = false;
        }

        ul_serve_n++;
        if (ul_serve_n % 1000 == 0)
            printf("[ul] %lu serves (data: %lu, zero: %lu, deferred-ok: %lu) "
                   "q=[%zu,%zu,%zu]\n",
                   (unsigned long)ul_serve_n, (unsigned long)ul_fresh,
                   (unsigned long)ul_zero, (unsigned long)ul_deferred_ok,
                   ul_q[0].size(), ul_q[1].size(), ul_q[2].size());
    };

    auto serve_ue_dl = [&](int i) {
        size_t idx = (size_t)(ue_dl_cursor[i] - dl_base);
        auto& chunk = dl_q[idx];
        zmq::message_t reply(chunk.size());
        memcpy(reply.data(), chunk.data(), chunk.size());
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
            dl_base++;
        }
    };

    printf("Running. Ctrl+C to stop.\n\n");

    try {
        while (g_running) {
            for (int i = 0; i < 5; i++) items[i].revents = 0;
            zmq::poll(items, 5, POLL_MS);

            /* ── 1. Collect UE UL into indexed FIFOs ── */
            for (int i = 0; i < NUM_UES; i++) {
                if (items[2 + i].revents & ZMQ_POLLIN) {
                    zmq::message_t msg;
                    ue_ul[i].recv(&msg);
                    UlChunk c;
                    c.idx = ul_rx_count[i]++;
                    c.data.assign((uint8_t*)msg.data(),
                                  (uint8_t*)msg.data() + msg.size());
                    ul_q[i].push_back(std::move(c));
                    ue_last_ul[i] = clk::now();

                    if (!ue_active[i]) {
                        ue_active[i] = true;
                        ue_dead[i]   = false;
                        /* Alignment anchor: UE's TX stream begins at its RX
                         * sample 0 = first DL chunk we served it. */
                        ul_align[i] = ue_dl_seen[i]
                                    ? (ue_dl_cursor[i] > 0 ? ue_dl_cursor[i] - 1
                                                           : 0)
                                    : dl_next;
                        /* cursor-1 only correct if exactly the first serve
                         * happened; the true anchor is recorded at first DL
                         * serve below — this is a fallback. */
                        printf("[align] ue%d first UL chunk. ul_align=%lu "
                               "(current serve=%lu, lag at start=%ld)\n",
                               i + 1, (unsigned long)ul_align[i],
                               (unsigned long)ul_serve_n,
                               (long)ul_serve_n - (long)ul_align[i]);
                    }
                    if (ul_q[i].size() > UL_Q_MAX) {
                        ul_q[i].pop_front();
                        if (++ul_dropped[i] % 50 == 1)
                            printf("[warn] ue%d UL q overflow (eNB stalled?)\n",
                                   i + 1);
                    }
                    send_req(ue_ul[i]);
                }
            }

            /* ── 1b. Dead-UE detection ── */
            for (int i = 0; i < NUM_UES; i++) {
                if (ue_active[i] && !ue_dead[i] &&
                    clk::now() - ue_last_ul[i] >
                        std::chrono::milliseconds(UE_DEAD_MS)) {
                    ue_dead[i] = true;
                    printf("[warn] ue%d silent >%dms — marked dead, "
                           "contributing zeros\n", i + 1, UE_DEAD_MS);
                }
            }

            /* ── 2. eNB UL request ── */
            if (items[1].revents & ZMQ_POLLIN) {
                zmq::message_t req;
                enb_ul.recv(&req);
                enb_ul_waiting = true;
                enb_ul_deadline = clk::now() + std::chrono::milliseconds(DEFER_MS);
            }
            /* ── 2b. Resolve held request ── */
            if (enb_ul_waiting) {
                if (any_ue_live()) {
                    if (ul_ready(ul_serve_n))
                        serve_enb_ul('F');
                    /* else: hold — never zero-insert while a UE is live.
                     * (Stalled-UE case handled by dead-marking above.) */
                } else if (clk::now() >= enb_ul_deadline) {
                    serve_enb_ul('Z');       /* pre-UE clocking */
                }
            }

            /* ── 3. eNB DL ── */
            if (items[0].revents & ZMQ_POLLIN) {
                zmq::message_t msg;
                enb_dl.recv(&msg);
                dl_q.emplace_back((uint8_t*)msg.data(),
                                  (uint8_t*)msg.data() + msg.size());
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
                    dl_base++;
                    dl_dropped++;
                }

                for (int i = 0; i < NUM_UES; i++)
                    if (ue_dl_pending[i] && ue_dl_cursor[i] < dl_next)
                        serve_ue_dl(i);
                trim_dl();
            }

            /* ── 4. UE DL requests ── */
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
                        /* THE alignment anchor: this UE's RX sample 0 is
                         * this DL chunk. Its TX chunk j must land at eNB
                         * UL serve (this seq + j). */
                        ul_align[i] = ue_dl_cursor[i];
                        printf("[align] ue%d first DL serve at seq %lu — "
                               "ul_align[%d]=%lu (eNB UL serve now %lu)\n",
                               i + 1, (unsigned long)ue_dl_cursor[i], i + 1,
                               (unsigned long)ul_align[i],
                               (unsigned long)ul_serve_n);
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

    printf("\n========== Final Stats ==========\n");
    printf("DL chunks:      %lu (dropped: %lu)\n",
           (unsigned long)dl_n, (unsigned long)dl_dropped);
    printf("UL serves:      %lu\n", (unsigned long)ul_serve_n);
    printf("  data:         %lu\n", (unsigned long)ul_fresh);
    printf("  zero (pre-UE clocking): %lu\n", (unsigned long)ul_zero);
    printf("  deferred→data:          %lu\n", (unsigned long)ul_deferred_ok);
    for (int i = 0; i < NUM_UES; i++)
        printf("ue%d: align=%lu rx=%lu dropped=%lu active=%d dead=%d\n",
               i + 1, (unsigned long)ul_align[i],
               (unsigned long)ul_rx_count[i],
               (unsigned long)ul_dropped[i],
               ue_active[i] ? 1 : 0, ue_dead[i] ? 1 : 0);
    printf("UL bursts seen: %d\n", burst_count);
    printf("=================================\n");

    enb_dl.close();
    enb_ul.close();
    for (int i = 0; i < NUM_UES; i++) { ue_dl[i].close(); ue_ul[i].close(); }
    ctx.close();
    printf("Done.\n");
    return 0;
}