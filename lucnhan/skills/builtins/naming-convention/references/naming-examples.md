# Naming Examples — Before / After

Concrete before/after examples for each common scenario. Load this reference when you want worked examples of the naming standard.

## 1. Single function rename (PE malware)

**Before** (`sub_4012A0`, calls `socket` → `connect` → `send`):
```c
int sub_4012A0(char *host, int port, char *data) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    connect(s, ...);
    send(s, data, ...);
    return s;
}
```
**After**: `ConnectAndSend` (confidence >90%).
If only 60% sure it's connect-and-send (no clear `connect`): `Unknown_NetSend_4012a0`.

## 2. Bulk rename batch (stripped Go binary)

Go functions recover as `sub_XXXX` because symbols are stripped, but `pclntab` leaks original names.
**Before**: `sub_4812F0`
**Recovered from pclntab**: `main.ConnectC2`
**After**: `go_main_ConnectC2`

## 3. Struct reconstruction + field naming

**Before**: a global pointer `dword_5A1000` accessed with offsets `+0`, `+8`, `+10h`.
**Reconstructed struct**:
```c
struct ConnectionConfig {
    char *server_url;        // +0x00
    int port;                // +0x08
    int timeout_ms;          // +0x10
};
```
**After**: struct `ConnectionConfig` (PascalCase), fields `server_url`/`port`/`timeout_ms` (snake_case).

## 4. Wrapper/thunk chain (IAT resolution)

**Call graph**: `sub_402000` → `__imp_CreateFileW`
- `sub_402000` body: just `jmp __imp_CreateFileW` (no frame) → `j_CreateFileW`
- If it had `push ebp; mov ebp,esp; call __imp_CreateFileW; pop ebp` → `thunk_CreateFileW`
- If it wrapped with a mutex lock around the call → `CreateFileWWrapper`

## 5. Crypto identification via magic constant

**Before**: `sub_4030B0` contains a 256-entry loop with byte swaps and a constant table starting `0x63, 0x7c, 0x77, 0x6b...`
**Evidence**: AES S-box bytes → near-certain AES.
**After**: `AesDecrypt` (or `AesEncrypt` depending on direction; the S-box alone doesn't tell direction — check for `InvMixColumns`/inverse S-box `0x52, 0x09, 0x6a...` to distinguish).

If direction is ambiguous (50-70%): `Unknown_AesOp_4030b0`.