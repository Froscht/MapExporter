/* C kernel for the Theia keystream. Mirrors the Python code in crypto.py exactly.
   cc -O2 -shared -fPIC -o theia_kern.so theia.c

   The keystream block counter is anchored to the FILE position within its 64 KiB page
   (page = key, (file_offset % PAGE) / 64 = counter). A container block may start in the
   middle of a page and of a keystream block, hence the skip argument. */
#include <stdint.h>
#include <string.h>

#ifdef _WIN32
#define THEIA_EXPORT __declspec(dllexport)
#else
#define THEIA_EXPORT
#endif

static const uint32_t IV[8] = {0x6A09E667,0xBB67AE85,0x3C6EF372,0xA54FF53A,0x510E527F,0x9B05688C,0x1F83D9AB,0x5BE0CD19};
static const int PERM[16] = {2,6,3,10,7,0,4,13,1,11,12,5,9,14,15,8};

static uint32_t ror32(uint32_t x, int n){ return (x>>n)|(x<<(32-n)); }

static void compress16(const uint32_t cv[8], const uint32_t block[16], uint64_t counter,
                       uint32_t block_len, uint32_t flags, uint32_t out[16]){
    uint32_t st[16];
    memcpy(st, cv, 32);
    memcpy(st+8, IV, 16);
    st[12] = (uint32_t)counter; st[13] = (uint32_t)(counter>>32);
    st[14] = block_len; st[15] = flags;
    uint32_t m[16]; memcpy(m, block, 64);
    for (int r = 0; r < 7; r++) {
        #define G(a,b,c,d,mx,my) \
            st[a] = st[a]+st[b]+m[mx]; \
            st[d] = ror32(st[d]^st[a],16); \
            st[c] = st[c]+st[d]; \
            st[b] = ror32(st[b]^st[c],12); \
            st[a] = st[a]+st[b]+m[my]; \
            st[d] = ror32(st[d]^st[a],8); \
            st[c] = st[c]+st[d]; \
            st[b] = ror32(st[b]^st[c],7);
        G(0,4,8,12,0,1); G(1,5,9,13,2,3); G(2,6,10,14,4,5); G(3,7,11,15,6,7);
        G(0,5,10,15,8,9); G(1,6,11,12,10,11); G(2,7,8,13,12,13); G(3,4,9,14,14,15);
        #undef G
        if (r < 6){ uint32_t p[16]; for (int i=0;i<16;i++) p[i]=m[PERM[i]]; memcpy(m,p,64); }
    }
    for (int i=0;i<8;i++){ out[i]=st[i]^st[i+8]; out[i+8]=st[i+8]^cv[i]; }
}

/* Decrypt one page range in place.
   state_words: the per-page state as 8 u32 words (first 8 words of init digest).
   block0:      keystream block index of the page containing the range start.
   skip:        byte offset of the range start inside that keystream block (0..63).
   The keystream is anchored to the FILE position, not to the buffer: data[0] belongs to
   keystream block block0 at byte skip. Block counters restart at 0 on every page; the page
   determines the key, the position within the page the block counter. */
THEIA_EXPORT void theia_decrypt_page(uint8_t *data, int len, const uint32_t state_words[8], uint32_t sb,
                        int block0, int skip){
    uint32_t zeros[16]; memset(zeros, 0, 64);
    int i = 0, blk = block0, off = skip;
    while (i < len) {
        uint32_t ks[16];
        compress16(state_words, zeros, (uint64_t)blk, sb, sb ^ 0x1Bu, ks);
        uint8_t *kb = (uint8_t*)ks;
        int n = 64 - off; if (n > len - i) n = len - i;
        for (int j = 0; j < n; j++) data[i + j] = (uint8_t)(data[i + j] - kb[off + j]);
        i += n; off = 0; blk++;
    }
}
