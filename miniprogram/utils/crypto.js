// 纯 JS 加密, 与 NAS 版 backend/app/services/backup.py 的备份加密**字节级互通**:
//   key = PBKDF2-HMAC-SHA256(passphrase, salt, 200000 次, 32 字节)
//   密文 = AES-256-GCM(key, nonce, data)  (输出 = ciphertext || 16字节tag)
//   备份文件 = "VSBK1"(5) | salt(16) | nonce(12) | 密文
//
// 全部纯 JS (小程序无 WebCrypto)。已在 node 里与 Python cryptography 互验。
// 注意: 小程序无密码学级随机源, salt/nonce 用 Math.random 生成 (个人自用工具可接受)。

// ----------------------------------------------------------------- SHA-256
const K256 = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
])

function _rotr(x, n) { return (x >>> n) | (x << (32 - n)) }

const _IV = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
const _W = new Uint32Array(64) // 复用 scratch, 减少 GC

// 压缩一个 64 字节块: 把 m[off..off+64) 吸收进状态 H (8 个 uint32, 原地更新)。
function _compress(H, m, off) {
  const w = _W
  for (let i = 0; i < 16; i++) {
    w[i] = (m[off + i * 4] << 24) | (m[off + i * 4 + 1] << 16) | (m[off + i * 4 + 2] << 8) | m[off + i * 4 + 3]
  }
  for (let i = 16; i < 64; i++) {
    const s0 = _rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3)
    const s1 = _rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10)
    w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0
  }
  let a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7]
  for (let i = 0; i < 64; i++) {
    const S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
    const ch = (e & f) ^ (~e & g)
    const t1 = (h + S1 + ch + K256[i] + w[i]) >>> 0
    const S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
    const maj = (a & b) ^ (a & c) ^ (b & c)
    const t2 = (S0 + maj) >>> 0
    h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0
  }
  H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0
  H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0
}

function _stateToBytes(H) {
  const out = new Uint8Array(32)
  for (let i = 0; i < 8; i++) {
    out[i * 4] = (H[i] >>> 24) & 0xff; out[i * 4 + 1] = (H[i] >>> 16) & 0xff
    out[i * 4 + 2] = (H[i] >>> 8) & 0xff; out[i * 4 + 3] = H[i] & 0xff
  }
  return out
}

function sha256(msg) {
  const len = msg.length
  const bitLen = len * 8
  const withOne = len + 1
  const total = withOne + ((56 - (withOne % 64) + 64) % 64) + 8
  const m = new Uint8Array(total)
  m.set(msg)
  m[len] = 0x80
  const hi = Math.floor(bitLen / 0x100000000)
  const lo = bitLen >>> 0
  m[total - 8] = (hi >>> 24) & 0xff; m[total - 7] = (hi >>> 16) & 0xff
  m[total - 6] = (hi >>> 8) & 0xff; m[total - 5] = hi & 0xff
  m[total - 4] = (lo >>> 24) & 0xff; m[total - 3] = (lo >>> 16) & 0xff
  m[total - 2] = (lo >>> 8) & 0xff; m[total - 1] = lo & 0xff
  const H = _IV.slice()
  for (let off = 0; off < total; off += 64) _compress(H, m, off)
  return _stateToBytes(H)
}

// ----------------------------------------------------------------- HMAC-SHA256
function hmacSha256(key, msg) {
  const B = 64
  let k = key
  if (k.length > B) k = sha256(k)
  const ipad = new Uint8Array(B), opad = new Uint8Array(B)
  for (let i = 0; i < B; i++) { const kb = i < k.length ? k[i] : 0; ipad[i] = kb ^ 0x36; opad[i] = kb ^ 0x5c }
  const inner = sha256(_concat(ipad, msg))
  return sha256(_concat(opad, inner))
}

function _concat(a, b) {
  const out = new Uint8Array(a.length + b.length)
  out.set(a); out.set(b, a.length)
  return out
}

// 从一个"已吸收前置块的状态"继续吸收 msg 并 finalize。prefixLen = 已吸收的字节数 (64 的倍数)。
// 用于 PBKDF2 把 ipad/opad 块的压缩结果缓存复用 (每次 PRF 省一半压缩)。
function _finalizeFrom(state, msg, prefixLen) {
  const H = state.slice()
  const total = prefixLen + msg.length
  const bitLen = total * 8
  // 对 msg 部分做填充: msg || 0x80 || 0..0 || 64bit 总长。
  const tailLen = msg.length + 1
  const padded = tailLen + ((56 - (tailLen % 64) + 64) % 64) + 8
  const m = new Uint8Array(padded)
  m.set(msg)
  m[msg.length] = 0x80
  const hi = Math.floor(bitLen / 0x100000000)
  const lo = bitLen >>> 0
  m[padded - 8] = (hi >>> 24) & 0xff; m[padded - 7] = (hi >>> 16) & 0xff
  m[padded - 6] = (hi >>> 8) & 0xff; m[padded - 5] = hi & 0xff
  m[padded - 4] = (lo >>> 24) & 0xff; m[padded - 3] = (lo >>> 16) & 0xff
  m[padded - 2] = (lo >>> 8) & 0xff; m[padded - 1] = lo & 0xff
  for (let off = 0; off < padded; off += 64) _compress(H, m, off)
  return _stateToBytes(H)
}

// ----------------------------------------------------------------- PBKDF2-HMAC-SHA256
// 优化: 缓存 ipad/opad 块的压缩状态, 每次迭代只需 2 次压缩 (而非 4 次)。
function pbkdf2Sha256(password, salt, iterations, dkLen) {
  const B = 64, hLen = 32
  let k = password
  if (k.length > B) k = sha256(k)
  const ipad = new Uint8Array(B), opad = new Uint8Array(B)
  for (let i = 0; i < B; i++) { const kb = i < k.length ? k[i] : 0; ipad[i] = kb ^ 0x36; opad[i] = kb ^ 0x5c }
  const ipadState = _IV.slice(); _compress(ipadState, ipad, 0)
  const opadState = _IV.slice(); _compress(opadState, opad, 0)
  const prf = (msg) => _finalizeFrom(opadState, _finalizeFrom(ipadState, msg, B), B)

  const blocks = Math.ceil(dkLen / hLen)
  const out = new Uint8Array(blocks * hLen)
  const saltBlock = new Uint8Array(salt.length + 4)
  saltBlock.set(salt)
  for (let i = 1; i <= blocks; i++) {
    saltBlock[salt.length] = (i >>> 24) & 0xff
    saltBlock[salt.length + 1] = (i >>> 16) & 0xff
    saltBlock[salt.length + 2] = (i >>> 8) & 0xff
    saltBlock[salt.length + 3] = i & 0xff
    let u = prf(saltBlock)
    const t = new Uint8Array(u)
    for (let j = 1; j < iterations; j++) {
      u = prf(u)
      for (let x = 0; x < hLen; x++) t[x] ^= u[x]
    }
    out.set(t, (i - 1) * hLen)
  }
  return out.subarray(0, dkLen)
}

// ----------------------------------------------------------------- AES-256
const SBOX = (() => {
  // 运行时生成 S-box, 避免长常量表。
  const sbox = new Uint8Array(256)
  let p = 1, q = 1
  do {
    p = p ^ (p << 1) ^ (p & 0x80 ? 0x11b : 0)
    p &= 0xff
    q ^= q << 1; q ^= q << 2; q ^= q << 4; q &= 0xff
    if (q & 0x80) q ^= 0x09
    q &= 0xff
    const xformed = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
    sbox[p] = (xformed ^ 0x63) & 0xff
  } while (p !== 1)
  sbox[0] = 0x63
  return sbox
})()

const RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d]

function _xtime(a) { return ((a << 1) ^ (a & 0x80 ? 0x1b : 0)) & 0xff }
function _mul(a, b) {
  let r = 0
  while (b) {
    if (b & 1) r ^= a
    a = _xtime(a)
    b >>= 1
  }
  return r & 0xff
}

function aesKeyExpansion(key) {
  // key: 32 bytes -> 60 words (Nr=14, 15 round keys)
  const Nk = 8, Nr = 14
  const w = new Array(4 * (Nr + 1))
  for (let i = 0; i < Nk; i++) {
    w[i] = [key[4 * i], key[4 * i + 1], key[4 * i + 2], key[4 * i + 3]]
  }
  for (let i = Nk; i < 4 * (Nr + 1); i++) {
    let t = w[i - 1].slice()
    if (i % Nk === 0) {
      t = [t[1], t[2], t[3], t[0]] // RotWord
      t = t.map((b) => SBOX[b]) // SubWord
      t[0] ^= RCON[i / Nk - 1]
    } else if (i % Nk === 4) {
      t = t.map((b) => SBOX[b])
    }
    w[i] = [w[i - Nk][0] ^ t[0], w[i - Nk][1] ^ t[1], w[i - Nk][2] ^ t[2], w[i - Nk][3] ^ t[3]]
  }
  return w
}

function aesEncryptBlock(w, input) {
  const Nr = 14
  // state column-major
  let s = [
    [input[0], input[4], input[8], input[12]],
    [input[1], input[5], input[9], input[13]],
    [input[2], input[6], input[10], input[14]],
    [input[3], input[7], input[11], input[15]],
  ]
  const addRoundKey = (round) => {
    for (let c = 0; c < 4; c++) {
      const wk = w[round * 4 + c]
      s[0][c] ^= wk[0]; s[1][c] ^= wk[1]; s[2][c] ^= wk[2]; s[3][c] ^= wk[3]
    }
  }
  addRoundKey(0)
  for (let round = 1; round <= Nr; round++) {
    // SubBytes
    for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) s[r][c] = SBOX[s[r][c]]
    // ShiftRows
    for (let r = 1; r < 4; r++) {
      const row = [s[r][0], s[r][1], s[r][2], s[r][3]]
      for (let c = 0; c < 4; c++) s[r][c] = row[(c + r) % 4]
    }
    // MixColumns (skip on last round)
    if (round !== Nr) {
      for (let c = 0; c < 4; c++) {
        const a0 = s[0][c], a1 = s[1][c], a2 = s[2][c], a3 = s[3][c]
        s[0][c] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
        s[1][c] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
        s[2][c] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
        s[3][c] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)
      }
    }
    addRoundKey(round)
  }
  const out = new Uint8Array(16)
  for (let c = 0; c < 4; c++) {
    out[4 * c] = s[0][c]; out[4 * c + 1] = s[1][c]; out[4 * c + 2] = s[2][c]; out[4 * c + 3] = s[3][c]
  }
  return out
}

// ----------------------------------------------------------------- GCM
function _gfMul(X, Y) {
  // 128-bit GF(2^128) multiply (bytes big-endian). Returns Uint8Array(16).
  const Z = new Uint8Array(16)
  const V = new Uint8Array(Y)
  for (let i = 0; i < 128; i++) {
    const bit = (X[i >> 3] >> (7 - (i & 7))) & 1
    if (bit) for (let j = 0; j < 16; j++) Z[j] ^= V[j]
    // V >>= 1, if lsb set then V ^= R (0xe1 << 120)
    let lsb = V[15] & 1
    for (let j = 15; j > 0; j--) V[j] = ((V[j] >> 1) | ((V[j - 1] & 1) << 7)) & 0xff
    V[0] = V[0] >> 1
    if (lsb) V[0] ^= 0xe1
  }
  return Z
}

function _ghash(H, data) {
  let Y = new Uint8Array(16)
  for (let i = 0; i < data.length; i += 16) {
    for (let j = 0; j < 16; j++) Y[j] ^= data[i + j] || 0
    Y = _gfMul(Y, H)
  }
  return Y
}

function _inc32(block) {
  const b = new Uint8Array(block)
  for (let i = 15; i >= 12; i--) {
    b[i] = (b[i] + 1) & 0xff
    if (b[i] !== 0) break
  }
  return b
}

function _ctr(w, icb, input) {
  const out = new Uint8Array(input.length)
  let cb = new Uint8Array(icb)
  for (let i = 0; i < input.length; i += 16) {
    const ks = aesEncryptBlock(w, cb)
    for (let j = 0; j < 16 && i + j < input.length; j++) out[i + j] = input[i + j] ^ ks[j]
    cb = _inc32(cb)
  }
  return out
}

function _lenBlock(aadLen, ctLen) {
  const b = new Uint8Array(16)
  const aBits = aadLen * 8, cBits = ctLen * 8
  // 64-bit big-endian each (our sizes fit in 32 bits)
  b[4] = (aBits >>> 24) & 0xff; b[5] = (aBits >>> 16) & 0xff; b[6] = (aBits >>> 8) & 0xff; b[7] = aBits & 0xff
  b[12] = (cBits >>> 24) & 0xff; b[13] = (cBits >>> 16) & 0xff; b[14] = (cBits >>> 8) & 0xff; b[15] = cBits & 0xff
  return b
}

function aesGcmEncrypt(key, iv, plaintext, aad) {
  aad = aad || new Uint8Array(0)
  const w = aesKeyExpansion(key)
  const H = aesEncryptBlock(w, new Uint8Array(16))
  // J0 for 12-byte IV = IV || 0x00000001
  const J0 = new Uint8Array(16)
  J0.set(iv); J0[15] = 1
  const ciphertext = _ctr(w, _inc32(J0), plaintext)
  // GHASH(aad padded || ct padded || lenblock)
  const ghashInput = _concat(_concat(_pad16(aad), _pad16(ciphertext)), _lenBlock(aad.length, ciphertext.length))
  const S = _ghash(H, ghashInput)
  const ej0 = aesEncryptBlock(w, J0)
  const tag = new Uint8Array(16)
  for (let i = 0; i < 16; i++) tag[i] = S[i] ^ ej0[i]
  return { ciphertext, tag }
}

function aesGcmDecrypt(key, iv, ciphertext, tag, aad) {
  aad = aad || new Uint8Array(0)
  const w = aesKeyExpansion(key)
  const H = aesEncryptBlock(w, new Uint8Array(16))
  const J0 = new Uint8Array(16)
  J0.set(iv); J0[15] = 1
  const ghashInput = _concat(_concat(_pad16(aad), _pad16(ciphertext)), _lenBlock(aad.length, ciphertext.length))
  const S = _ghash(H, ghashInput)
  const ej0 = aesEncryptBlock(w, J0)
  let ok = 0
  for (let i = 0; i < 16; i++) ok |= (S[i] ^ ej0[i]) ^ tag[i]
  if (ok !== 0) throw new Error('解密失败: 口令错误或备份包损坏 (GCM tag 校验不通过)')
  return _ctr(w, _inc32(J0), ciphertext)
}

function _pad16(a) {
  const rem = a.length % 16
  if (rem === 0) return a
  const out = new Uint8Array(a.length + (16 - rem))
  out.set(a)
  return out
}

// ----------------------------------------------------------------- 高层: 备份加解密 (VSBK1)
const MAGIC = [0x56, 0x53, 0x42, 0x4b, 0x31] // "VSBK1"
const ITERS = 200000

function _utf8(str) {
  const out = []
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i)
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f))
    else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
  }
  return new Uint8Array(out)
}

// randomBytes: 小程序无密码学随机, 用 Math.random (个人工具可接受)。node 测试可注入。
let _randomImpl = (n) => {
  const b = new Uint8Array(n)
  for (let i = 0; i < n; i++) b[i] = (Math.random() * 256) & 0xff
  return b
}
function setRandom(fn) { _randomImpl = fn }

function encryptBackup(dataBytes, passphrase) {
  const salt = _randomImpl(16)
  const nonce = _randomImpl(12)
  const key = pbkdf2Sha256(_utf8(passphrase), salt, ITERS, 32)
  const { ciphertext, tag } = aesGcmEncrypt(key, nonce, dataBytes)
  const out = new Uint8Array(5 + 16 + 12 + ciphertext.length + 16)
  out.set(MAGIC, 0)
  out.set(salt, 5)
  out.set(nonce, 21)
  out.set(ciphertext, 33)
  out.set(tag, 33 + ciphertext.length)
  return out
}

function isEncrypted(blob) {
  return blob.length >= 5 && MAGIC.every((m, i) => blob[i] === m)
}

function decryptBackup(blob, passphrase) {
  if (!isEncrypted(blob)) throw new Error('不是有效的加密备份包 (magic 不匹配)')
  const salt = blob.subarray(5, 21)
  const nonce = blob.subarray(21, 33)
  const ctWithTag = blob.subarray(33)
  const ciphertext = ctWithTag.subarray(0, ctWithTag.length - 16)
  const tag = ctWithTag.subarray(ctWithTag.length - 16)
  const key = pbkdf2Sha256(_utf8(passphrase), salt, ITERS, 32)
  return aesGcmDecrypt(key, nonce, ciphertext, tag)
}

module.exports = {
  sha256, hmacSha256, pbkdf2Sha256,
  aesKeyExpansion, aesEncryptBlock, aesGcmEncrypt, aesGcmDecrypt,
  encryptBackup, decryptBackup, isEncrypted, setRandom,
}
