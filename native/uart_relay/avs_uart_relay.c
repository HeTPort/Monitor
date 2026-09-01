#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

#define RELAY_VERSION "1.0.1"
#define DEFAULT_MAX_FRAME 512U
#define DEFAULT_TAIL_GUARD 64U
#define MAX_TAIL_GUARD 4096U
#define INPUT_CAPACITY 4096U

static uint32_t crc32_bytes(const unsigned char *data, size_t length) {
    uint32_t crc = 0xffffffffU;
    size_t i;
    for (i = 0; i < length; ++i) {
        unsigned bit;
        crc ^= data[i];
        for (bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return crc ^ 0xffffffffU;
}

static size_t cobs_encode(const unsigned char *input, size_t length,
                          unsigned char *output, size_t capacity) {
    size_t read_index = 0, write_index = 1, code_index = 0;
    unsigned char code = 1;
    if (capacity == 0) return 0;
    while (read_index < length) {
        if (input[read_index] == 0) {
            output[code_index] = code;
            code_index = write_index++;
            code = 1;
            ++read_index;
        } else {
            if (write_index >= capacity) return 0;
            output[write_index++] = input[read_index++];
            if (++code == 0xff) {
                output[code_index] = code;
                code_index = write_index++;
                code = 1;
            }
        }
        if (write_index > capacity) return 0;
    }
    output[code_index] = code;
    return write_index;
}

static size_t cobs_decode(const unsigned char *input, size_t length,
                          unsigned char *output, size_t capacity) {
    size_t read_index = 0, write_index = 0;
    while (read_index < length) {
        unsigned char code = input[read_index++];
        size_t count;
        if (code == 0 || read_index + (size_t)code - 1 > length) return 0;
        count = (size_t)code - 1;
        if (write_index + count + ((code != 0xff && read_index + count < length) ? 1U : 0U) > capacity)
            return 0;
        memcpy(output + write_index, input + read_index, count);
        write_index += count;
        read_index += count;
        if (code != 0xff && read_index < length) output[write_index++] = 0;
    }
    return write_index;
}

static int write_all(int fd, const unsigned char *data, size_t length) {
    size_t offset = 0;
    while (offset < length) {
        ssize_t written = write(fd, data + offset, length - offset);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return -1;
        offset += (size_t)written;
    }
    return 0;
}

static int write_nul_guard(int fd, size_t length) {
    static const unsigned char zeros[64] = {0};
    while (length > 0) {
        size_t chunk = length < sizeof(zeros) ? length : sizeof(zeros);
        if (write_all(fd, zeros, chunk) != 0) return -1;
        length -= chunk;
    }
    return 0;
}

static int baud_constant(long baud, speed_t *speed) {
    switch (baud) {
#ifdef B1200
        case 1200: *speed = B1200; return 0;
#endif
#ifdef B2400
        case 2400: *speed = B2400; return 0;
#endif
#ifdef B4800
        case 4800: *speed = B4800; return 0;
#endif
#ifdef B9600
        case 9600: *speed = B9600; return 0;
#endif
#ifdef B19200
        case 19200: *speed = B19200; return 0;
#endif
#ifdef B38400
        case 38400: *speed = B38400; return 0;
#endif
#ifdef B57600
        case 57600: *speed = B57600; return 0;
#endif
#ifdef B115200
        case 115200: *speed = B115200; return 0;
#endif
#ifdef B230400
        case 230400: *speed = B230400; return 0;
#endif
        default: return -1;
    }
}

static int configure_uart(int fd, long baud, struct termios *original) {
    struct termios config;
    speed_t speed;
    if (baud_constant(baud, &speed) != 0) {
        fprintf(stderr, "avs-uart-relay: unsupported baud rate: %ld\n", baud);
        return -1;
    }
    if (tcgetattr(fd, original) != 0) return -1;
    config = *original;
    config.c_iflag = 0;
    config.c_oflag = 0;
    config.c_lflag = 0;
    config.c_cflag &= ~(CSIZE | PARENB | CSTOPB);
    config.c_cflag |= CS8 | CLOCAL | CREAD;
    config.c_cc[VMIN] = 0;
    config.c_cc[VTIME] = 0;
    if (cfsetispeed(&config, speed) != 0 || cfsetospeed(&config, speed) != 0) return -1;
    return tcsetattr(fd, TCSANOW, &config);
}

static int self_test(void) {
    static const unsigned char sample[] = "123456789";
    unsigned char input[] = {0, 1, 2, 0, 3};
    unsigned char encoded[16];
    unsigned char decoded[16];
    size_t size = cobs_encode(input, sizeof(input), encoded, sizeof(encoded));
    size_t decoded_size = cobs_decode(encoded, size, decoded, sizeof(decoded));
    if (crc32_bytes(sample, sizeof(sample) - 1) != 0xcbf43926U ||
        size == 0 || decoded_size != sizeof(input) || memcmp(decoded, input, sizeof(input)) != 0)
        return 1;
    printf("{\"relay\":\"avs-uart-relay\",\"version\":\"%s\",\"self_test\":true,\"termios\":true,\"tcdrain\":true}\n", RELAY_VERSION);
    return 0;
}

static int relay_stream(int fd, size_t max_frame, size_t tail_guard) {
    char line[INPUT_CAPACITY];
    unsigned char payload[INPUT_CAPACITY + 4];
    unsigned char encoded[INPUT_CAPACITY + 32];
    const unsigned char delimiter = 0;
    while (fgets(line, sizeof(line), stdin) != NULL) {
        size_t length = strlen(line);
        uint32_t crc;
        size_t encoded_length;
        if (length && line[length - 1] == '\n') line[--length] = '\0';
        if (length && line[length - 1] == '\r') line[--length] = '\0';
        if (length == 0) continue;
        if (!strchr(line, '{') || line[0] != '{') {
            fprintf(stderr, "avs-uart-relay: input is not a JSON object\n");
            return 4;
        }
        memcpy(payload, line, length);
        crc = crc32_bytes(payload, length);
        payload[length] = (unsigned char)(crc & 0xffU);
        payload[length + 1] = (unsigned char)((crc >> 8) & 0xffU);
        payload[length + 2] = (unsigned char)((crc >> 16) & 0xffU);
        payload[length + 3] = (unsigned char)((crc >> 24) & 0xffU);
        encoded_length = cobs_encode(payload, length + 4, encoded, sizeof(encoded));
        if (encoded_length == 0 || encoded_length > max_frame) {
            fprintf(stderr, "avs-uart-relay: encoded frame exceeds %zu bytes\n", max_frame);
            return 4;
        }
        if (write_all(fd, &delimiter, 1) != 0 ||
            write_all(fd, encoded, encoded_length) != 0 ||
            write_all(fd, &delimiter, 1) != 0 || tcdrain(fd) != 0) {
            perror("avs-uart-relay: UART write/drain");
            return 3;
        }
    }
    if (ferror(stdin)) return 3;
    /*
     * Some UART/DMA drivers acknowledge tcdrain() while retaining a short
     * terminal tail until a later write.  NUL is the COBS frame delimiter, so
     * an EOF-only NUL guard safely pushes the final frame out; only disposable
     * empty frames may remain buffered after close.
     */
    if (write_nul_guard(fd, tail_guard) != 0 || tcdrain(fd) != 0) {
        perror("avs-uart-relay: UART EOF guard/drain");
        return 3;
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *uart = NULL;
    long baud = 9600;
    size_t max_frame = DEFAULT_MAX_FRAME;
    size_t tail_guard = DEFAULT_TAIL_GUARD;
    int check_only = 0, fd, result;
    struct termios original;
    int i;
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        printf("avs-uart-relay %s protocol uart-v2\n", RELAY_VERSION);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) return self_test();
    for (i = 1; i < argc; ++i) {
        if ((strcmp(argv[i], "--uart") == 0 || strcmp(argv[i], "--check-uart") == 0) && i + 1 < argc) {
            check_only = strcmp(argv[i], "--check-uart") == 0;
            uart = argv[++i];
        } else if (strcmp(argv[i], "--baud") == 0 && i + 1 < argc) {
            baud = strtol(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--max-frame") == 0 && i + 1 < argc) {
            max_frame = (size_t)strtoul(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--tail-guard") == 0 && i + 1 < argc) {
            tail_guard = (size_t)strtoul(argv[++i], NULL, 10);
        } else {
            fprintf(stderr, "usage: avs-uart-relay (--uart PATH|--check-uart PATH) [--baud N] [--max-frame N] [--tail-guard N]\n");
            return 2;
        }
    }
    if (uart == NULL || baud <= 0 || max_frame < 64 || max_frame > INPUT_CAPACITY ||
        tail_guard > MAX_TAIL_GUARD) return 2;
    fd = open(uart, O_WRONLY | O_NOCTTY);
    if (fd < 0) { perror("avs-uart-relay: open UART"); return 3; }
    if (configure_uart(fd, baud, &original) != 0) {
        perror("avs-uart-relay: configure UART"); close(fd); return 3;
    }
    if (check_only) {
        result = tcdrain(fd) == 0 ? 0 : 3;
        printf("{\"uart\":\"%s\",\"baudrate\":%ld,\"open\":true,\"termios\":true,\"tcdrain\":%s}\n",
               uart, baud, result == 0 ? "true" : "false");
    } else {
        result = relay_stream(fd, max_frame, tail_guard);
    }
    (void)tcsetattr(fd, TCSANOW, &original);
    close(fd);
    return result;
}
