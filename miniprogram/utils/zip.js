// 纯 JS ZIP 读写 (无依赖)。只用 STORED (不压缩) 方式写 —— 这样 NAS 版后端的 Python
// zipfile 能直接读小程序产出的备份 (mini -> NAS 跨端可恢复)。读取时支持 STORED;
// 遇到 DEFLATE 压缩条目 (如后端默认产出的包) 抛友好错误 (需解压库, 见 README)。
//
// 所有数据用 Uint8Array。文件名按 UTF-8 编码 (flag bit 11 置位)。

// ---- UTF-8 (小程序无 TextEncoder 也能用) ----
function utf8Encode(str) {
  const out = []
  for (let i = 0; i < str.length; i++) {
    let c = str.charCodeAt(i)
    if (c < 0x80) out.push(c)
    else if (c < 0x800) {
      out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f))
    } else if (c >= 0xd800 && c <= 0xdbff) {
      // surrogate pair
      const c2 = str.charCodeAt(++i)
      c = 0x10000 + ((c & 0x3ff) << 10) + (c2 & 0x3ff)
      out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 0x3f), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
    } else {
      out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
    }
  }
  return new Uint8Array(out)
}

function utf8Decode(bytes) {
  let out = ''
  let i = 0
  while (i < bytes.length) {
    const c = bytes[i++]
    if (c < 0x80) out += String.fromCharCode(c)
    else if (c < 0xe0) out += String.fromCharCode(((c & 0x1f) << 6) | (bytes[i++] & 0x3f))
    else if (c < 0xf0)
      out += String.fromCharCode(((c & 0x0f) << 12) | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f))
    else {
      const cp = ((c & 0x07) << 18) | ((bytes[i++] & 0x3f) << 12) | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f)
      const u = cp - 0x10000
      out += String.fromCharCode(0xd800 + (u >> 10), 0xdc00 + (u & 0x3ff))
    }
  }
  return out
}

// ---- CRC32 ----
const CRC_TABLE = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()

function crc32(bytes) {
  let c = 0xffffffff
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

// ---- 写 ----
function _w16(arr, v) { arr.push(v & 0xff, (v >> 8) & 0xff) }
function _w32(arr, v) { arr.push(v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, (v >>> 24) & 0xff) }

// files: { name: Uint8Array | string }。返回 Uint8Array。
function zipStore(files) {
  const local = []
  const central = []
  let offset = 0
  const DOSDATE = 0x21 // 1980-01-01
  const FLAG = 0x0800 // UTF-8 filename

  Object.keys(files).forEach((name) => {
    let data = files[name]
    if (typeof data === 'string') data = utf8Encode(data)
    const nameBytes = utf8Encode(name)
    const crc = crc32(data)

    const lh = []
    _w32(lh, 0x04034b50)
    _w16(lh, 20); _w16(lh, FLAG); _w16(lh, 0) // version, flag, method=stored
    _w16(lh, 0); _w16(lh, DOSDATE) // time, date
    _w32(lh, crc); _w32(lh, data.length); _w32(lh, data.length)
    _w16(lh, nameBytes.length); _w16(lh, 0) // name len, extra len
    const lhArr = new Uint8Array(lh)
    local.push(lhArr, nameBytes, data)
    const localLen = lhArr.length + nameBytes.length + data.length

    const ch = []
    _w32(ch, 0x02014b50)
    _w16(ch, 20); _w16(ch, 20); _w16(ch, FLAG); _w16(ch, 0)
    _w16(ch, 0); _w16(ch, DOSDATE)
    _w32(ch, crc); _w32(ch, data.length); _w32(ch, data.length)
    _w16(ch, nameBytes.length); _w16(ch, 0); _w16(ch, 0) // name, extra, comment
    _w16(ch, 0); _w16(ch, 0); _w32(ch, 0) // disk, internal, external attrs
    _w32(ch, offset)
    central.push(new Uint8Array(ch), nameBytes)

    offset += localLen
  })

  const centralStart = offset
  let centralSize = 0
  central.forEach((c) => (centralSize += c.length))

  const eocd = []
  _w32(eocd, 0x06054b50)
  _w16(eocd, 0); _w16(eocd, 0)
  const n = Object.keys(files).length
  _w16(eocd, n); _w16(eocd, n)
  _w32(eocd, centralSize); _w32(eocd, centralStart)
  _w16(eocd, 0)

  const parts = [...local, ...central, new Uint8Array(eocd)]
  let total = 0
  parts.forEach((p) => (total += p.length))
  const out = new Uint8Array(total)
  let pos = 0
  parts.forEach((p) => { out.set(p, pos); pos += p.length })
  return out
}

// ---- DEFLATE inflate (raw, 无 zlib 头) ----
// 标准 RFC1951 解压, 用于读取 NAS 版后端产出的 DEFLATE 压缩条目 (实现 NAS->mini 反向恢复)。
// 实现参考 puff/tiny-inflate 算法, 纯 JS 无依赖。
function _buildHuff(lengths) {
  let max = 0
  for (let i = 0; i < lengths.length; i++) if (lengths[i] > max) max = lengths[i]
  const blCount = new Array(max + 1).fill(0)
  for (let i = 0; i < lengths.length; i++) blCount[lengths[i]]++
  blCount[0] = 0
  const next = new Array(max + 1).fill(0)
  let code = 0
  for (let bits = 1; bits <= max; bits++) {
    code = (code + blCount[bits - 1]) << 1
    next[bits] = code
  }
  // map: "len:code" -> symbol, 让 decodeSym O(1)/bit (避免每 bit 线性扫描)。
  const map = {}
  for (let n = 0; n < lengths.length; n++) {
    const len = lengths[n]
    if (len) { map[len + ':' + next[len]] = n; next[len]++ }
  }
  return { map, max }
}

const LEN_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258]
const LEN_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
const DIST_BASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577]
const DIST_EXTRA = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]
const CLEN_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]

function rawInflate(input) {
  const out = []
  let bitpos = 0
  function getBit() {
    const b = (input[bitpos >> 3] >> (bitpos & 7)) & 1
    bitpos++
    return b
  }
  function getBits(n) {
    let v = 0
    for (let i = 0; i < n; i++) v |= getBit() << i
    return v
  }
  function decodeSym(huff) {
    let code = 0
    let len = 0
    while (true) {
      code = (code << 1) | getBit()
      len++
      const sym = huff.map[len + ':' + code]
      if (sym !== undefined) return sym
      if (len > huff.max) throw new Error('inflate: 无效 Huffman 码')
    }
  }
  let last = 0
  while (!last) {
    last = getBit()
    const type = getBits(2)
    if (type === 0) {
      // stored: 字节对齐, LEN(2) + NLEN(2, LEN 的反码) + 原始数据
      bitpos = (bitpos + 7) & ~7
      const lo = bitpos >> 3
      const len = input[lo] | (input[lo + 1] << 8)
      const nlen = input[lo + 2] | (input[lo + 3] << 8)
      if ((len ^ 0xffff) !== nlen) throw new Error('inflate: stored 块 NLEN 校验失败')
      bitpos += 32
      const start = bitpos >> 3
      for (let i = 0; i < len; i++) out.push(input[start + i])
      bitpos += len * 8
    } else {
      let litHuff
      let distHuff
      if (type === 1) {
        const litLens = []
        for (let i = 0; i < 144; i++) litLens.push(8)
        for (let i = 144; i < 256; i++) litLens.push(9)
        for (let i = 256; i < 280; i++) litLens.push(7)
        for (let i = 280; i < 288; i++) litLens.push(8)
        litHuff = _buildHuff(litLens)
        distHuff = _buildHuff(new Array(30).fill(5))
      } else {
        const hlit = getBits(5) + 257
        const hdist = getBits(5) + 1
        const hclen = getBits(4) + 4
        const clen = new Array(19).fill(0)
        for (let i = 0; i < hclen; i++) clen[CLEN_ORDER[i]] = getBits(3)
        const clenHuff = _buildHuff(clen)
        const lens = []
        while (lens.length < hlit + hdist) {
          const sym = decodeSym(clenHuff)
          if (sym < 16) lens.push(sym)
          else if (sym === 16) {
            const r = getBits(2) + 3
            const prev = lens[lens.length - 1]
            for (let i = 0; i < r; i++) lens.push(prev)
          } else if (sym === 17) {
            const r = getBits(3) + 3
            for (let i = 0; i < r; i++) lens.push(0)
          } else {
            const r = getBits(7) + 11
            for (let i = 0; i < r; i++) lens.push(0)
          }
        }
        litHuff = _buildHuff(lens.slice(0, hlit))
        distHuff = _buildHuff(lens.slice(hlit))
      }
      while (true) {
        const sym = decodeSym(litHuff)
        if (sym === 256) break
        if (sym < 256) out.push(sym)
        else {
          const li = sym - 257
          const length = LEN_BASE[li] + getBits(LEN_EXTRA[li])
          const dsym = decodeSym(distHuff)
          const dist = DIST_BASE[dsym] + getBits(DIST_EXTRA[dsym])
          const from = out.length - dist
          for (let i = 0; i < length; i++) out.push(out[from + i])
        }
      }
    }
  }
  return new Uint8Array(out)
}

// ---- 读 ----
function _r16(b, o) { return b[o] | (b[o + 1] << 8) }
function _r32(b, o) { return (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0 }

// 返回 { name: Uint8Array }。opts.filter(name) 返回 false 的条目跳过 (不解压, 省时)。
function unzip(buf, opts) {
  const filter = opts && opts.filter
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  // 从尾部找 EOCD
  let eocd = -1
  for (let i = b.length - 22; i >= 0; i--) {
    if (_r32(b, i) === 0x06054b50) { eocd = i; break }
  }
  if (eocd < 0) throw new Error('不是有效的 ZIP (找不到 EOCD)')
  const count = _r16(b, eocd + 10)
  let p = _r32(b, eocd + 16) // central dir offset
  const out = {}
  for (let i = 0; i < count; i++) {
    if (_r32(b, p) !== 0x02014b50) throw new Error('ZIP 中央目录损坏')
    const method = _r16(b, p + 10)
    const compSize = _r32(b, p + 20)
    const nameLen = _r16(b, p + 28)
    const extraLen = _r16(b, p + 30)
    const commentLen = _r16(b, p + 32)
    const localOff = _r32(b, p + 42)
    const name = utf8Decode(b.subarray(p + 46, p + 46 + nameLen))
    // 定位本地头里的数据
    const lNameLen = _r16(b, localOff + 26)
    const lExtraLen = _r16(b, localOff + 28)
    if (!filter || filter(name)) {
      const dataStart = localOff + 30 + lNameLen + lExtraLen
      const raw = b.subarray(dataStart, dataStart + compSize)
      if (method === 0) {
        out[name] = raw
      } else if (method === 8) {
        out[name] = rawInflate(raw) // DEFLATE: 读取 NAS 版后端产出的压缩包
      } else {
        throw new Error(`条目 ${name} 用了不支持的压缩方式 (method=${method})`)
      }
    }
    p += 46 + nameLen + extraLen + commentLen
  }
  return out
}

module.exports = { zipStore, unzip, crc32, utf8Encode, utf8Decode, rawInflate }
