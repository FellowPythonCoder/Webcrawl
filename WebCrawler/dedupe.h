/* dedupe.h — adaptive Bloom filter for M4 / 50KB-per-1000 goal
 *
 * Old engine used fixed 512 MB filter. New:
 *  - Starts at 4 MB (2^25 bits), grows exponentially as needed
 *  - Chain of filters, each 2x larger than previous, max 512 MB total
 *  - For 1000 sites: ~4 MB RAM, for 100M: ~512 MB, auto-scaling
 *  - Exact dedupe still done by SQLite PRIMARY KEY on disk
 *  - Zero per-domain heap allocation, lock-free reads
 *
 * 50KB per 1000 is achieved on disk via SQLite tuning (page_size=512),
 * not by bloom. Bloom is RAM-only and freed on exit.
 */
#ifndef DEDUPE_H
#define DEDUPE_H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#define BLOOM_K 4
#define BLOOM_INIT_LOG2 25          /* 32M bits = 4 MB */
#define BLOOM_MAX_LOG2 32           /* 4B bits = 512 MB */
#define BLOOM_MAX_FILTERS 6

typedef struct {
    uint64_t *bits;
    uint64_t nbits;     /* power of two */
    uint64_t mask;
    uint64_t count;     /* insertions */
    uint64_t words;
} BloomOne;

static BloomOne bloom_chain[BLOOM_MAX_FILTERS];
static int bloom_chain_len = 0;
static uint64_t bloom_total_inserts = 0;

static uint64_t dd_fnv(const char *s) {
    uint64_t h = 1469598103934665603ULL;
    while (*s) {
        h ^= (unsigned char)*s++;
        h *= 1099511628211ULL;
    }
    return h;
}
static uint64_t dd_mix(uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

static int bloom_one_init(BloomOne *b, int log2_bits) {
    b->nbits = 1ULL << log2_bits;
    b->mask = b->nbits - 1;
    b->words = b->nbits >> 6;
    b->count = 0;
    b->bits = (uint64_t *)calloc((size_t)b->words, sizeof(uint64_t));
    return b->bits != NULL;
}

static void bloom_one_free(BloomOne *b) {
    if (b->bits) free(b->bits);
    b->bits = NULL;
}

static inline int bloom_one_test(BloomOne *b, uint64_t h1, uint64_t h2) {
    for (int k = 0; k < BLOOM_K; k++) {
        uint64_t idx = (h1 + (uint64_t)k * h2) & b->mask;
        if (!(b->bits[idx >> 6] & (1ULL << (idx & 63)))) return 0;
    }
    return 1;
}
static inline void bloom_one_set(BloomOne *b, uint64_t h1, uint64_t h2) {
    for (int k = 0; k < BLOOM_K; k++) {
        uint64_t idx = (h1 + (uint64_t)k * h2) & b->mask;
        b->bits[idx >> 6] |= (1ULL << (idx & 63));
    }
    b->count++;
}

static int bloom_init(void) {
    bloom_chain_len = 1;
    if (!bloom_one_init(&bloom_chain[0], BLOOM_INIT_LOG2)) return 0;
    for (int i = 1; i < BLOOM_MAX_FILTERS; i++) {
        bloom_chain[i].bits = NULL;
        bloom_chain[i].nbits = 0;
    }
    fprintf(stderr, "[Bloom] init %llu bits (%.1f MB)\n",
            (unsigned long long)bloom_chain[0].nbits,
            bloom_chain[0].nbits / 8.0 / 1024 / 1024);
    return 1;
}

/* Grow chain if load > 15% */
static void bloom_maybe_grow(void) {
    if (bloom_chain_len >= BLOOM_MAX_FILTERS) return;
    BloomOne *cur = &bloom_chain[bloom_chain_len - 1];
    if (cur->nbits == 0) return;
    /* load factor = count * K / nbits */
    double load = (double)cur->count * BLOOM_K / (double)cur->nbits;
    if (load < 0.15) return;
    int cur_log2 = 0;
    uint64_t nb = cur->nbits;
    while (nb > 1) { nb >>= 1; cur_log2++; }
    if (cur_log2 >= BLOOM_MAX_LOG2) return;
    int next_log2 = cur_log2 + 1;
    if (next_log2 > BLOOM_MAX_LOG2) next_log2 = BLOOM_MAX_LOG2;
    BloomOne *next = &bloom_chain[bloom_chain_len];
    if (!bloom_one_init(next, next_log2)) return;
    bloom_chain_len++;
    fprintf(stderr, "[Bloom] grow -> filter %d: %llu bits (%.1f MB), chain=%d, total inserts=%llu\n",
            bloom_chain_len, (unsigned long long)next->nbits,
            next->nbits / 8.0 / 1024 / 1024,
            bloom_chain_len,
            (unsigned long long)bloom_total_inserts);
}

/* returns 1 if probably seen, 0 if newly inserted */
static int bloom_test_and_set(const char *key) {
    uint64_t h = dd_fnv(key);
    uint64_t h1 = dd_mix(h);
    uint64_t h2 = dd_mix(h ^ 0xa5a5a5a5deadbeefULL) | 1ULL;

    for (int i = 0; i < bloom_chain_len; i++) {
        if (bloom_chain[i].bits && bloom_one_test(&bloom_chain[i], h1, h2)) {
            return 1;
        }
    }
    /* not seen: insert into newest filter */
    BloomOne *dst = &bloom_chain[bloom_chain_len - 1];
    if (dst->bits) {
        bloom_one_set(dst, h1, h2);
        bloom_total_inserts++;
        bloom_maybe_grow();
    }
    return 0;
}

static void bloom_stats(void) {
    fprintf(stderr, "[Bloom] chain=%d total_inserts=%llu\n",
            bloom_chain_len, (unsigned long long)bloom_total_inserts);
    for (int i = 0; i < bloom_chain_len; i++) {
        if (bloom_chain[i].bits) {
            double load = (double)bloom_chain[i].count * BLOOM_K / (double)bloom_chain[i].nbits * 100.0;
            fprintf(stderr, "  filter %d: %.1f MB, %llu inserts, load %.2f%%\n",
                    i, bloom_chain[i].nbits / 8.0 / 1024 / 1024,
                    (unsigned long long)bloom_chain[i].count, load);
        }
    }
}

#endif
