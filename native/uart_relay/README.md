# Native UART relay

`avs_uart_relay.c` is deliberately limited to ISO C11 plus POSIX `termios`.
It has no C++, Rust, shell-tool, or third-party runtime dependency.

Build it with the same OpenHarmony Clang target/sysroot/ABI used for the CPU
and GPU workloads, then stage the result at the platform-configured
`relay.local_asset`. Do not copy a desktop Linux binary to the device.

Host sanity check (on a POSIX host):

```sh
cc -std=c11 -Wall -Wextra -Werror -O2 -o avs-uart-relay avs_uart_relay.c
./avs-uart-relay --self-test
```

Device checks are exposed by `vmin_judge relay probe --platform PLATFORM`.
They do not transmit a payload: version, self-test, and UART termios/tcdrain
checks run independently of a workload.
