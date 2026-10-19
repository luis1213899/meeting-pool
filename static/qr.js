/* 纯 JS QR 码生成器：字节模式，版本 1-4，纠错级别 M。
   零依赖。输出模块矩阵供 canvas 渲染。
   腾讯会议入会链接通常 40 字节左右，V3（42 字节）即可覆盖。 */
window.QR = (function () {
  'use strict';

  // ---- GF(256) 指数/对数表 ----
  const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
  (function () {
    let x = 1;
    for (let i = 0; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  })();
  function mul(a, b) { return (!a || !b) ? 0 : EXP[LOG[a] + LOG[b]]; }

  function genPoly(n) {
    let p = [1];
    for (let i = 0; i < n; i++) {
      const next = new Array(p.length + 1).fill(0);
      for (let j = 0; j < p.length; j++) { next[j] ^= p[j]; next[j + 1] ^= mul(p[j], EXP[i]); }
      p = next;
    }
    return p;
  }
  function rsEncode(data, ecLen) {
    const g = genPoly(ecLen);
    const res = new Array(ecLen).fill(0);
    for (let k = 0; k < data.length; k++) {
      const f = data[k] ^ res[0];
      res.shift(); res.push(0);
      if (f) for (let j = 0; j < ecLen; j++) res[j] ^= mul(g[j + 1], f);
    }
    return res;
  }

  // 版本 1-4，纠错级别 M：[总数据码字, [[块数,每块数据码字]], 每块纠错码字]
  const VER = { 1: [16, [[1, 16]], 10], 2: [28, [[1, 28]], 16], 3: [44, [[1, 44]], 26], 4: [64, [[2, 32]], 18] };
  const CAP = { 1: 14, 2: 26, 3: 42, 4: 62 };
  const ALIGN = { 2: [18], 3: [22], 4: [26] };

  function pickVersion(n) { for (let v = 1; v <= 4; v++) if (n <= CAP[v]) return v; return 0; }

  function strToBytes(s) {
    const out = [];
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
      else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
    }
    return out;
  }

  function encodeBytes(bytes, ver) {
    const total = VER[ver][0];
    const bits = [];
    const push = (v, n) => { for (let i = n - 1; i >= 0; i--) bits.push((v >> i) & 1); };
    push(0b0100, 4);                       // 字节模式
    push(bytes.length, 8);                 // 计数（V1-9 用 8 位）
    for (const b of bytes) push(b, 8);
    push(0, 4);                             // 终止符
    while (bits.length % 8) bits.push(0);   // 补到字节边界
    const cw = [];
    for (let i = 0; i < bits.length; i += 8) {
      let v = 0; for (let j = 0; j < 8; j++) v = (v << 1) | bits[i + j];
      cw.push(v);
    }
    const pad = [0xec, 0x11];
    let p = 0;
    while (cw.length < total) cw.push(pad[p++ & 1]);
    return cw;
  }

  function interleave(dataCw, ver) {
    const groups = VER[ver][1], ecLen = VER[ver][2];
    const blocks = [];
    let idx = 0;
    for (const [cnt, per] of groups) for (let b = 0; b < cnt; b++) { blocks.push(dataCw.slice(idx, idx + per)); idx += per; }
    const ec = blocks.map((d) => rsEncode(d, ecLen));
    const maxLen = Math.max(...blocks.map((b) => b.length));
    const out = [];
    for (let i = 0; i < maxLen; i++) for (const b of blocks) if (i < b.length) out.push(b[i]);
    for (let i = 0; i < ecLen; i++) for (const e of ec) out.push(e[i]);
    const bits = [];
    for (const cw of out) for (let i = 7; i >= 0; i--) bits.push((cw >> i) & 1);
    return bits;
  }

  function makeMatrix(size) {
    const m = []; for (let i = 0; i < size; i++) m.push(new Uint8Array(size)); return m;
  }

  function placeFinder(m, r, row, col) {
    for (let dr = -1; dr <= 7; dr++) for (let dc = -1; dc <= 7; dc++) {
      const rr = row + dr, cc = col + dc;
      if (rr < 0 || cc < 0 || rr >= m.length || cc >= m.length) continue;
      r[rr][cc] = 1;
      if (dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6) {
        const ring = (dr === 0 || dr === 6 || dc === 0 || dc === 6);
        const core = (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4);
        m[rr][cc] = (ring || core) ? 1 : 0;
      }
    }
  }

  function placeTiming(m, r, size) {
    for (let i = 8; i < size - 8; i++) {
      r[6][i] = r[i][6] = 1;
      m[6][i] = (i & 1) ? 0 : 1;
      m[i][6] = (i & 1) ? 0 : 1;
    }
  }

  function placeAlign(m, r, ver) {
    const pos = ALIGN[ver] || [];
    const size = m.length;
    for (const cx of pos) for (const cy of pos) {
      if ((cx <= 8 && cy <= 8) || (cx >= size - 9 && cy <= 8) || (cx <= 8 && cy >= size - 9)) continue;
      for (let dr = -2; dr <= 2; dr++) for (let dc = -2; dc <= 2; dc++) {
        const rr = cy + dr, cc = cx + dc;
        r[rr][cc] = 1;
        const ring = (Math.abs(dr) === 2 || Math.abs(dc) === 2);
        m[rr][cc] = ring ? 1 : (dr === 0 && dc === 0 ? 1 : 0);
      }
    }
  }

  function reserveFmt(r, size) {
    for (let i = 0; i <= 8; i++) { r[8][i] = 1; r[i][8] = 1; }
    for (let i = 0; i < 8; i++) r[8][size - 1 - i] = 1;
    for (let i = 0; i < 7; i++) r[size - 1 - i][8] = 1;
    r[size - 8][8] = 1;
  }

  function placeData(m, r, size, bits) {
    let bi = 0, up = true;
    for (let col = size - 1; col > 0; col -= 2) {
      if (col === 6) col--;
      for (let i = 0; i < size; i++) {
        const row = up ? (size - 1 - i) : i;
        for (let c = 0; c < 2; c++) {
          const cc = col - c;
          if (!r[row][cc] && bi < bits.length) m[row][cc] = bits[bi++];
        }
      }
      up = !up;
    }
  }

  const MASKS = [
    (r, c) => (r + c) % 2 === 0,
    (r, c) => r % 2 === 0,
    (r, c) => c % 3 === 0,
    (r, c) => (r + c) % 3 === 0,
    (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
    (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
    (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
    (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
  ];

  function applyMask(m, r, size, mask) {
    const m2 = m.map((row) => row.slice());
    for (let i = 0; i < size; i++) for (let j = 0; j < size; j++) {
      if (!r[i][j] && mask(i, j)) m2[i][j] = m[i][j] ^ 1;
    }
    return m2;
  }

  function penalty(m, size) {
    let p = 0;
    // N1: 连续 5 个以上同色
    for (let i = 0; i < size; i++) {
      let run = 1;
      for (let j = 1; j < size; j++) {
        if (m[i][j] === m[i][j - 1]) run++;
        else { if (run >= 5) p += 3 + (run - 5); run = 1; }
      }
      if (run >= 5) p += 3 + (run - 5);
    }
    for (let j = 0; j < size; j++) {
      let run = 1;
      for (let i = 1; i < size; i++) {
        if (m[i][j] === m[i - 1][j]) run++;
        else { if (run >= 5) p += 3 + (run - 5); run = 1; }
      }
      if (run >= 5) p += 3 + (run - 5);
    }
    // N2: 2x2 同色块
    for (let i = 0; i < size - 1; i++) for (let j = 0; j < size - 1; j++) {
      if (m[i][j] === m[i][j + 1] && m[i][j] === m[i + 1][j] && m[i][j] === m[i + 1][j + 1]) p += 3;
    }
    // N3: finder 似样 1011101，前或后跟 4 个浅色
    const pat = [1, 0, 1, 1, 1, 0, 1];
    for (let i = 0; i < size; i++) for (let j = 0; j <= size - 7; j++) {
      let match = true;
      for (let k = 0; k < 7; k++) if (m[i][j + k] !== pat[k]) { match = false; break; }
      if (!match) continue;
      let before = j - 4 >= 0;
      if (before) for (let k = 1; k <= 4; k++) if (m[i][j - k] !== 0) { before = false; break; }
      let after = j + 7 + 4 <= size;
      if (after) for (let k = 0; k < 4; k++) if (m[i][j + 7 + k] !== 0) { after = false; break; }
      if (before || after) p += 40;
    }
    for (let j = 0; j < size; j++) for (let i = 0; i <= size - 7; i++) {
      let match = true;
      for (let k = 0; k < 7; k++) if (m[i + k][j] !== pat[k]) { match = false; break; }
      if (!match) continue;
      let before = i - 4 >= 0;
      if (before) for (let k = 1; k <= 4; k++) if (m[i - k][j] !== 0) { before = false; break; }
      let after = i + 7 + 4 <= size;
      if (after) for (let k = 0; k < 4; k++) if (m[i + 7 + k][j] !== 0) { after = false; break; }
      if (before || after) p += 40;
    }
    // N4: 黑白比例
    let dark = 0;
    for (let i = 0; i < size; i++) for (let j = 0; j < size; j++) if (m[i][j]) dark++;
    const pct = (dark * 100) / (size * size);
    p += 10 * Math.floor(Math.abs(50 - pct) / 5);
    return p;
  }

  function formatBits(maskId) {
    const data = (0b00 << 3) | maskId;      // EC level M = 00
    let rem = data << 10;
    const g = 0x537;                        // x^10+x^8+x^5+x^4+x^2+x+1
    for (let i = 14; i >= 10; i--) if ((rem >> i) & 1) rem ^= g << (i - 10);
    return ((data << 10) | (rem & 0x3ff)) ^ 0x5412;
  }

  function placeFormat(m, size, maskId) {
    const f = formatBits(maskId);
    for (let i = 0; i < 6; i++) m[8][i] = (f >> i) & 1;       // bits 0-5
    m[8][7] = (f >> 6) & 1;                                   // bit 6（跳过 8,6 计时）
    m[8][8] = (f >> 7) & 1;                                   // bit 7
    m[7][8] = (f >> 8) & 1;                                   // bit 8
    m[5][8] = (f >> 9) & 1;                                   // bit 9（跳过 6,8 计时）
    for (let i = 0; i < 5; i++) m[4 - i][8] = (f >> (10 + i)) & 1; // bits 10-14
    for (let i = 0; i < 7; i++) m[size - 1 - i][8] = (f >> i) & 1;   // bits 0-6（左下）
    for (let i = 0; i < 8; i++) m[8][size - 8 + i] = (f >> (7 + i)) & 1; // bits 7-14（右上）
    m[size - 8][8] = 1;                                       // 固定暗模块
  }

  function generate(text) {
    const bytes = strToBytes(text);
    const ver = pickVersion(bytes.length);
    if (!ver) return null;
    const size = 4 * ver + 17;
    const m = makeMatrix(size), r = makeMatrix(size);
    placeFinder(m, r, 0, 0);
    placeFinder(m, r, 0, size - 7);
    placeFinder(m, r, size - 7, 0);
    placeTiming(m, r, size);
    placeAlign(m, r, ver);
    reserveFmt(r, size);
    const bits = interleave(encodeBytes(bytes, ver), ver);
    placeData(m, r, size, bits);
    let best = null, bestP = Infinity;
    for (let mk = 0; mk < 8; mk++) {
      const mm = applyMask(m, r, size, MASKS[mk]);
      placeFormat(mm, size, mk);
      const p = penalty(mm, size);
      if (p < bestP) { bestP = p; best = mm; }
    }
    return { size, matrix: best };
  }

  function toCanvas(text, canvas, scale) {
    const res = generate(text);
    if (!res) return false;
    const { size, matrix } = res;
    const quiet = 4;
    const s = scale || 8;
    canvas.width = canvas.height = (size + quiet * 2) * s;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#000';
    for (let i = 0; i < size; i++) for (let j = 0; j < size; j++) {
      if (matrix[i][j]) ctx.fillRect((j + quiet) * s, (i + quiet) * s, s, s);
    }
    return true;
  }

  return { generate, toCanvas, CAP, pickVersion };
})();
