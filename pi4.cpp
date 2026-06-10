#include <Arduino.h>

extern const uint8_t pi_data_bin_start[] asm("_binary_src_pi_data_bin_start");
extern const uint8_t pi_data_bin_end[]   asm("_binary_src_pi_data_bin_end");

static uint16_t bcd_lut[256];
static bool lut_init = false;

void run_pi(const char* tag, const char* payload) {
    int N, M;
    if (sscanf(payload, "%d %d", &N, &M) == 2) {
        static char out_buf[4096];
        int buf_idx = 0;

        const char* t = tag;
        while (*t) out_buf[buf_idx++] = *t++;
        out_buf[buf_idx++] = ']';
        
        int start_idx = N - 1;
        int length = (M - 1) - start_idx + 1;
        
        if (length > 0) {
            const uint8_t* ptr = pi_data_bin_start + (start_idx >> 1);
            int remaining = length;
            
            if ((start_idx & 1) != 0 && remaining > 0) {
                out_buf[buf_idx++] = '0' + ((*ptr) & 0x0F);
                ptr++; remaining--;
            }
            
            if (!lut_init) {
                for (int i = 0; i < 256; i++) {
                    uint8_t hi = i >> 4;
                    uint8_t lo = i & 0x0F;
                    bcd_lut[i] = ('0' + hi) | (('0' + lo) << 8); 
                }
                lut_init = true;
            }

            while (remaining >= 2) {
                if (buf_idx >= 4094) {
                    Serial.write((const uint8_t*)out_buf, buf_idx); 
                    buf_idx = 0;
                }
                *(uint16_t*)(&out_buf[buf_idx]) = bcd_lut[*ptr++];
                buf_idx += 2;
                remaining -= 2;
            }
            
            if (remaining > 0) {
                if (buf_idx >= 4095) {
                    Serial.write((const uint8_t*)out_buf, buf_idx);
                    buf_idx = 0;
                }
                out_buf[buf_idx++] = '0' + ((*ptr) >> 4);
            }
        }
        
        if (buf_idx > 4096 - 7) {
            Serial.write((const uint8_t*)out_buf, buf_idx);
            buf_idx = 0;
        }
        const char* end_str = "[END]\r\n";
        while (*end_str) out_buf[buf_idx++] = *end_str++;
        
        if (buf_idx > 0) Serial.write((const uint8_t*)out_buf, buf_idx);
        Serial.flush();
    }
}