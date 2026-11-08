"""
LoRa PHY Decoder -- inverse of the LoRa PHY chain (same design as
lora_trx_sim.grc, EPB #4).

Input : int16 symbol values (FFT argmax bins) from the stock Argmax block.
        A 'frame_start' tag (set by the Frame Sync block, carried through the
        stock Multiply/FFT/Mag^2/Argmax blocks) marks the first symbol of a frame.

Per symbol:   bin -> (bin - 1) mod N -> Gray code -> (>> 2 for reduced rate)
Per block:    de-interleave (undo the diagonal) -> Hamming decode -> nibbles
Header block: 8 symbols, SF-2 bits, CR 4/8 -> length, CR, CRC flag, checksum.
              Result is sent back to Frame Sync ('frame_info') so it knows how
              many more symbols to cut out of the stream.
Payload:      nibbles (low first) -> bytes -> de-whiten -> CRC-16 check.

Message out 'frame_info': {'n_sym': n} or {'abort': True}
Message out 'payload'   : PDU (meta dict, u8vector) for every decoded frame.
"""
import numpy as np
import pmt
from gnuradio import gr


def lora_n_payload_symbols(sf, cr, payload_len, has_crc, ldro):
    num = 8 * payload_len - 4 * sf + 28 + 16 * int(bool(has_crc))
    den = 4 * (sf - 2 * int(bool(ldro)))
    return 8 + max(-(-num // den) * (cr + 4), 0)


def hamming_enc(nib, cr_app):                       # identical to the TX encoder
    d = [(nib >> 3) & 1, (nib >> 2) & 1, (nib >> 1) & 1, nib & 1]
    if cr_app == 1:
        p4 = d[0] ^ d[1] ^ d[2] ^ d[3]
        return (d[3] << 4) | (d[2] << 3) | (d[1] << 2) | (d[0] << 1) | p4
    p0 = d[3] ^ d[2] ^ d[1]
    p1 = d[2] ^ d[1] ^ d[0]
    p2 = d[3] ^ d[2] ^ d[0]
    p3 = d[3] ^ d[1] ^ d[0]
    cw = ((d[3] << 7) | (d[2] << 6) | (d[1] << 5) | (d[0] << 4) |
          (p0 << 3) | (p1 << 2) | (p2 << 1) | p3)
    return cw >> (4 - cr_app)


class blk(gr.sync_block):
    """LoRa PHY Decoder: Gray, de-interleave, Hamming, header, de-whiten, CRC"""

    def __init__(self, sf=12, ldro=1, verbose=True):
        gr.sync_block.__init__(self, name='LoRa PHY Decoder',
                               in_sig=[np.int16], out_sig=None)
        self.sf = int(sf)
        self.N = 1 << self.sf
        self.ldro = bool(ldro)
        self.verbose = bool(verbose)
        seq, reg = [], 0xFF
        for _ in range(255):
            seq.append(reg)
            fb = ((reg >> 7) ^ (reg >> 5) ^ (reg >> 4) ^ (reg >> 3)) & 1
            reg = ((reg << 1) & 0xFF) | fb
        self.whitening_seq = np.array(seq, dtype=np.uint8)
        # code books for nearest-codeword decoding
        self.book = {c: [hamming_enc(n, c) for n in range(16)] for c in (1, 2, 3, 4)}
        self.message_port_register_out(pmt.intern('frame_info'))
        self.message_port_register_out(pmt.intern('payload'))
        self.syms = None
        self.hdr = None
        self.meta = {}
        self.stats = {'frames': 0, 'crc_ok': 0, 'crc_bad': 0, 'hdr_bad': 0}

    # ---------------- symbol -> bits ----------------
    def sym_to_word(self, s, reduced):
        v = (int(s) - 1) % self.N          # undo the +1 shift
        v ^= v >> 1                        # Gray code (undoes TX inverse-Gray)
        return v >> 2 if reduced else v    # reduced rate: drop the 2 LSBs

    @staticmethod
    def deinterleave(words, sf_app, cw_len):
        cws = []
        for k in range(sf_app):
            c = 0
            for i in range(cw_len):
                j = (i - k - 1) % sf_app
                c = (c << 1) | ((words[i] >> (sf_app - 1 - j)) & 1)
            cws.append(c)
        return cws

    def hamming_dec(self, cw, cr_app):
        if cr_app >= 3:                    # 4/7, 4/8: correct 1 bit (nearest codeword)
            dists = [bin(cw ^ c).count('1') for c in self.book[cr_app]]
            return int(np.argmin(dists))
        top = cw >> cr_app                 # 4/5, 4/6: detection only, just read data bits
        return ((top >> 3) & 1) | (((top >> 2) & 1) << 1) | (((top >> 1) & 1) << 2) | ((top & 1) << 3)

    def block_nibbles(self, syms, sf_app, cr_app):
        reduced = sf_app != self.sf
        words = [self.sym_to_word(s, reduced) for s in syms]
        return [self.hamming_dec(c, cr_app)
                for c in self.deinterleave(words, sf_app, cr_app + 4)]

    @staticmethod
    def crc16(data):
        crc = 0
        for byte in data:
            byte = int(byte)
            for _ in range(8):
                if ((crc & 0x8000) >> 8) ^ (byte & 0x80):
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
                byte = (byte << 1) & 0xFF
        return crc

    # ---------------- frame logic ----------------
    def parse_header(self):
        nib = self.block_nibbles(self.syms[:8], self.sf - 2, 4)
        h0, h1, h2, c4n, c30 = nib[:5]
        b = lambda v, k: (v >> k) & 1
        c4 = b(h0, 3) ^ b(h0, 2) ^ b(h0, 1) ^ b(h0, 0)
        c3 = b(h0, 3) ^ b(h1, 3) ^ b(h1, 2) ^ b(h1, 1) ^ b(h2, 0)
        c2 = b(h0, 2) ^ b(h1, 3) ^ b(h1, 0) ^ b(h2, 3) ^ b(h2, 1)
        c1 = b(h0, 1) ^ b(h1, 2) ^ b(h1, 0) ^ b(h2, 2) ^ b(h2, 1) ^ b(h2, 0)
        c0 = b(h0, 0) ^ b(h1, 1) ^ b(h2, 3) ^ b(h2, 2) ^ b(h2, 1) ^ b(h2, 0)
        L, cr, crc = (h0 << 4) | h1, h2 >> 1, h2 & 1
        if (c4n & 1) != c4 or c30 != ((c3 << 3) | (c2 << 2) | (c1 << 1) | c0) \
                or L == 0 or not 1 <= cr <= 4:
            return None
        return {'len': L, 'cr': cr, 'crc': crc, 'nibs': nib[5:],
                'n_sym': lora_n_payload_symbols(self.sf, cr, L, crc, self.ldro)}

    def finish_frame(self):
        h = self.hdr
        nibs = list(h['nibs'])
        sf_app = self.sf - 2 if self.ldro else self.sf
        cw_len = 4 + h['cr']
        rest = self.syms[8:h['n_sym']]
        for i in range(0, len(rest), cw_len):
            nibs += self.block_nibbles(rest[i:i + cw_len], sf_app, h['cr'])
        L = h['len']
        wh = np.array([nibs[2 * i] | (nibs[2 * i + 1] << 4) for i in range(L)], dtype=np.uint8)
        payload = wh ^ self.whitening_seq[:L]
        crc_ok = None
        if h['crc']:
            cn = nibs[2 * L:2 * L + 4]
            rx_crc = cn[0] | (cn[1] << 4) | (cn[2] << 8) | (cn[3] << 12)
            calc = self.crc16(payload[:-2]) ^ int(payload[-1]) ^ (int(payload[-2]) << 8)
            crc_ok = rx_crc == calc
        self.stats['frames'] += 1
        self.stats['crc_ok' if crc_ok in (True, None) else 'crc_bad'] += 1
        meta = dict(self.meta)
        meta.update({'len': L, 'cr': '4/%d' % (4 + h['cr']),
                     'crc': {True: 'OK', False: 'FAIL', None: 'none'}[crc_ok]})
        self.message_port_pub(pmt.intern('payload'),
                              pmt.cons(pmt.to_pmt(meta), pmt.init_u8vector(L, payload.tolist())))
        if self.verbose:
            print('[LoRa RX] frame %d  len %d  CR %s  CRC %s  CFO %+.0f Hz  SNR %.1f dB'
                  % (self.stats['frames'], L, meta['cr'], meta['crc'],
                     meta.get('cfo_hz', 0.0), meta.get('snr_db', 0.0)), flush=True)

    def work(self, input_items, output_items):
        inp = input_items[0]
        base = self.nitems_read(0)
        starts = {t.offset - base: t for t in
                  self.get_tags_in_window(0, 0, len(inp), pmt.intern('frame_start'))}
        for i in range(len(inp)):
            if i in starts:
                self.syms, self.hdr = [], None
                try:
                    self.meta = pmt.to_python(starts[i].value)
                except Exception:
                    self.meta = {}
            if self.syms is None:
                continue
            self.syms.append(int(inp[i]))
            if self.hdr is None and len(self.syms) == 8:
                self.hdr = self.parse_header()
                if self.hdr is None:
                    self.stats['hdr_bad'] += 1
                    if self.verbose:
                        print('[LoRa RX] header checksum failed', flush=True)
                    self.message_port_pub(pmt.intern('frame_info'),
                                          pmt.to_pmt({'abort': True}))
                    self.syms = None
                    continue
                self.message_port_pub(pmt.intern('frame_info'),
                                      pmt.to_pmt({'n_sym': self.hdr['n_sym']}))
            if self.hdr is not None and len(self.syms) == self.hdr['n_sym']:
                self.finish_frame()
                self.syms, self.hdr = None, None
        return len(inp)
