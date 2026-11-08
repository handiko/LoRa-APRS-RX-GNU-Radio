"""
LoRa Frame Synchroniser -- the one part of a LoRa receiver GNU Radio has no
stock block for: deciding WHERE each symbol starts and HOW FAR off-tune we are.
(Same design as lora_trx_sim.grc, EPB #3.)

Input : complex baseband at os samples per chip (samp_rate = os * bw)
Output: a complex STREAM, emitted in blocks of N = 2^SF samples, ONE BLOCK PER
        PAYLOAD SYMBOL (a stock Stream to Vector N block downstream turns each
        block into one vector). Each block is already
        - aligned to the symbol boundary (integer + fractional timing),
        - decimated to 1 sample per chip at the best sampling phase,
        - frequency-corrected (fractional + integer CFO),
        - drift-corrected (sampling-clock offset derived from the CFO).
        The de-chirp, FFT and argmax that turn these vectors into symbol
        values are done DOWNSTREAM by stock blocks (Multiply Const, FFT,
        Complex to Mag^2, Argmax).
        The first sample of every frame carries a 'frame_start' stream tag.
        (Why a stream and not a vector port: GRC sizes Embedded-Python ports
        from the constructor's DEFAULT arguments, so a vector port would stay
        at the default SF's length and turn the connection red for other SFs.)

Message in 'frame_info': the header decoder answers {'n_sym': n} (total symbols
        incl. the 8 header symbols) or {'abort': True} after the first 8.

State machine:
  DETECT  free-running de-chirp (window grid not aligned to anything).
          A preamble = several consecutive windows whose FFT peak sits in
          the SAME bin.  That bin = timing offset + CFO (mixed together).
  ALIGN   (instant) fractional CFO from the peak's phase rotation between
          windows; fractional timing by trying all os sampling phases;
          shift the window grid by the peak bin -> up-chirps now land in bin 0.
  SFD     step symbol by symbol, de-chirping each window with BOTH chirp
          directions.  Sync-word chirps show up as bins (8*hi, 8*lo).
          The first window where the DOWN-chirp de-chirp wins is the SFD;
          its bin = 2 x integer CFO  -> CFO and timing finally separated.
  OUTPUT  emit 8 header symbols, wait for the header decoder's length,
          emit the rest, go back to DETECT.
"""
import numpy as np
import pmt
from gnuradio import gr


class blk(gr.basic_block):
    """LoRa Frame Sync: preamble detect, CFO/STO estimation, SFD, symbol windows"""

    def __init__(self, sf=12, bw=125e3, samp_rate=500e3, sync_word=0x12,
                 preamble_len=8, center_freq=432.15e6, sro_comp=True, track=True,
                 verbose=True):
        self.sf = int(sf)
        self.N = 1 << self.sf
        gr.basic_block.__init__(self, name='LoRa Frame Sync',
                                in_sig=[np.complex64],
                                out_sig=[np.complex64])
        self.set_output_multiple(self.N)            # always emit whole symbols
        self.bw = float(bw)
        self.fs = float(samp_rate)
        os_f = self.fs / self.bw
        assert abs(os_f - round(os_f)) < 1e-9 and os_f >= 1, \
            'samp_rate must be an integer multiple of bw'
        self.os = int(round(os_f))
        self.L = self.N * self.os                    # input samples per symbol
        self.preamble_len = int(preamble_len)
        sw = int(sync_word) & 0xFF
        self.sync_syms = ((sw >> 4) << 3, (sw & 0x0F) << 3)
        self.fc = float(center_freq)
        self.sro_comp = bool(sro_comp) and self.fc > 0
        self.verbose = bool(verbose)
        self.track = bool(track)
        # Frequency tracking through the payload (2nd-order loop, units = FFT bins).
        # Real trackers drift tens of Hz during a 3 s SF12 frame (PA warms the crystal);
        # 1 bin is only 30.5 Hz at SF12/125k, so a frequency frozen at the preamble
        # pushes late symbols into the neighbouring bin.
        self.trk_g1 = 0.3                             # phase-detector -> frequency gain
        self.trk_g2 = 0.02                            # -> frequency-rate gain (follows ramps)
        self.trk_gate = 2.0 * np.log(self.N)          # only learn from clearly-decided symbols
        self.kk = np.arange(self.N)

        n = np.arange(self.N)
        self.up = np.exp(2j * np.pi * (n * n / (2.0 * self.N) - n / 2.0))  # base up-chirp, 1 sample/chip
        self.down = np.conj(self.up)

        self.DET = 3                                  # consecutive matching windows = preamble
        self.thr = 1.5 * np.log(self.N)               # min peak-to-mean ratio of a window
        # noise alone passes thr in ~N*exp(-thr) = 1.5 % of windows (SF12); three in a
        # row within +-1 bin of each other: ~1e-12 per window -> no false alarms.

        self.buf = np.zeros(0, dtype=np.complex64)
        self.buf_start = 0                            # absolute index of buf[0]
        self.pos = 0.0                                # absolute index of next window start
        self.reset()

        self.message_port_register_in(pmt.intern('frame_info'))
        self.set_msg_handler(pmt.intern('frame_info'), self.handle_frame_info)
        self.set_tag_propagation_policy(gr.TPP_DONT)
        self.frames = 0

    # ------------------------------------------------------------ helpers
    def reset(self):
        self.state = 'DETECT'
        self.hist = []          # DETECT: (start, Y, k) of recent windows
        self.cfo_norm = 0.0     # applied CFO correction, cycles per input sample
        self.up_bins = []       # SFD search: bins of up-chirp windows seen
        self.L_eff = float(self.L)
        self.trk_f = 0.0        # tracked extra frequency correction [bins]
        self.trk_r = 0.0        # tracked drift rate [bins/symbol]
        self.trk_hist = []
        self.n_out = 0
        self.n_total = None
        self.info = {}

    def handle_frame_info(self, msg):
        d = pmt.to_python(msg)
        if self.state != 'OUTPUT':
            return
        if d.get('abort', False):
            if self.verbose:
                print('[LoRa sync] header rejected -> back to DETECT', flush=True)
            self.reset()
        else:
            self.n_total = int(d['n_sym'])

    def have(self, start, length):
        s = int(round(start))
        return s >= self.buf_start and s + length <= self.buf_start + len(self.buf)

    def window(self, start, phase=0):
        """N samples, one per chip, starting at absolute sample start+phase, CFO-corrected."""
        idx = int(round(start)) + phase + np.arange(self.N) * self.os
        x = self.buf[idx - self.buf_start].astype(np.complex128)
        if self.cfo_norm != 0.0:
            x = x * np.exp(-2j * np.pi * ((self.cfo_norm * idx) % 1.0))
        return x

    def spec_up(self, x):     # de-chirp UP-chirps (multiply by down-chirp), FFT
        return np.fft.fft(x * self.down)

    def spec_down(self, x):   # de-chirp DOWN-chirps (multiply by up-chirp), FFT
        return np.fft.fft(x * self.up)

    def signed(self, k):
        return k - self.N if k >= self.N // 2 else k

    # ------------------------------------------------------------ states
    def step_detect(self):
        if not self.have(self.pos, self.L + self.os):
            return False
        Y = self.spec_up(self.window(self.pos))
        P = np.abs(Y) ** 2
        k = int(np.argmax(P))
        ratio = P[k] / (np.mean(P) + 1e-30)
        if ratio > self.thr:
            self.hist.append((self.pos, Y, k))
            self.hist = self.hist[-self.DET:]
        else:
            self.hist = []
        if len(self.hist) == self.DET:
            ks = [h[2] for h in self.hist]
            ok = all(abs(self.signed((ks[i + 1] - ks[i]) % self.N)) <= 1
                     for i in range(self.DET - 1))
            if ok:
                self.align()
                return True
            self.hist = self.hist[1:]
        self.pos += self.L
        return True

    def align(self):
        # (1) fractional CFO: the preamble is periodic, so window i+1 is window i
        #     rotated by 2*pi*CFO[bins].  Only the fraction of a bin is visible.
        k0 = self.hist[-1][2]
        z = sum(self.hist[i + 1][1][k0] * np.conj(self.hist[i][1][k0])
                for i in range(self.DET - 1))
        cfo_frac = np.angle(z) / (2 * np.pi)                     # bins, (-0.5, 0.5]
        self.cfo_norm = cfo_frac / self.L                         # cycles / input sample

        # (2) fractional timing: try every sampling phase, keep the sharpest peak
        w = self.hist[-1][0]
        best = (-1.0, 0, 0)
        for d in range(self.os):
            P = np.abs(self.spec_up(self.window(w, d))) ** 2 + \
                np.abs(self.spec_up(self.window(w - self.L, d))) ** 2
            k = int(np.argmax(P))
            if P[k] > best[0]:
                best = (P[k], d, k)
        Pk, d, k = best
        P = np.abs(self.spec_up(self.window(w, d))) ** 2
        noise = (np.sum(P) - P[k]) / (self.N - 1)
        snr = 10 * np.log10(max(P[k] - noise, 1e-12) / (self.N * noise + 1e-30))

        # (3) integer alignment: peak bin k = timing offset + integer CFO (chips).
        #     Shift the grid by k chips so preamble up-chirps de-chirp to bin 0.
        self.pos = w + d - self.signed(k) * self.os
        self.info = {'cfo_frac_bins': float(cfo_frac), 'preamble_bin': int(k),
                     'phase': int(d), 'snr_db': float(snr)}
        self.state = 'SFD'
        self.up_bins = []
        self.align_pos = self.pos                 # reference instant for drift correction

    def step_sfd(self):
        if not self.have(self.pos, self.L + self.os):
            return False
        x = self.window(self.pos)
        Pu = np.abs(self.spec_up(x)) ** 2
        Pd = np.abs(self.spec_down(x)) ** 2
        ku, kd = int(np.argmax(Pu)), int(np.argmax(Pd))
        if Pd[kd] <= Pu[ku]:                       # still an up-chirp (preamble / sync)
            self.up_bins.append(ku)
            if len(self.up_bins) > self.preamble_len + 4:
                self.reset()                       # never found an SFD: false alarm
            self.pos += self.L
            return True

        # ---- SFD found.
        # Since ALIGN the grid has stepped by the nominal L, so clock drift has moved
        # it by d chips (0.25 chip/symbol at SF12 per 60 ppm).  Up-chirps read s + d,
        # the SFD down-chirp reads 2*CFO - d.  d is measured from the sync word.
        sync = tuple(self.up_bins[-2:])
        err = [self.signed((sync[i] - self.sync_syms[i]) % self.N) for i in range(len(sync))]
        d = float(np.mean(err)) if len(err) == 2 else 0.0
        sw_ok = len(sync) == 2 and abs(d) <= 3.5 and all(abs(e - d) <= 1 for e in err)
        f_int = int(np.round((self.signed(kd) + d) / 2.0))
        if not sw_ok:
            if self.verbose:
                print('[LoRa sync] SFD found but sync word bins %s != %s -> ignored'
                      % (sync, self.sync_syms), flush=True)
            self.pos += self.L
            self.reset()
            return True

        self.cfo_norm += f_int / float(self.L)
        cfo_hz = self.cfo_norm * self.fs
        # The grid we aligned on was early by f_int chips (CFO masqueraded as timing).
        # While searching for the SFD we stepped by the nominal L; the real symbol
        # length is L_eff (TX and RX each derive carrier AND chip clock from one
        # crystal -> relative clock error = -CFO/fc), so the payload start is
        # recomputed from the alignment instant with L_eff.
        if self.sro_comp:
            self.L_eff = self.L * (1.0 - cfo_hz / self.fc)
        n_steps = int(round((self.pos - self.align_pos) / self.L))
        self.pos = self.align_pos + f_int * self.os + (n_steps + 2.25) * self.L_eff
        self.info.update({'cfo_int_bins': f_int, 'cfo_hz': float(cfo_hz),
                          'sync_bins': [int(b) for b in sync], 'drift_chips': d})
        self.state = 'OUTPUT'
        self.n_out, self.n_total = 0, None
        self.frames += 1
        if self.verbose:
            print('[LoRa sync] frame %d: SNR %.1f dB, CFO %+.0f Hz (%d + %.2f bins), '
                  'sync word bins %s (drift %+.1f chips), sampling phase %d/%d'
                  % (self.frames, self.info['snr_db'], cfo_hz, f_int,
                     self.info['cfo_frac_bins'], list(sync), d, self.info['phase'], self.os),
                  flush=True)
        return True

    def step_output(self, out, nout):
        limit = 8 if self.n_total is None else self.n_total
        if self.n_out >= limit:
            if self.n_total is not None:            # frame complete
                if self.verbose and self.track and self.trk_hist:
                    print('[LoRa sync] frame %d: frequency tracked %+.0f Hz over the payload'
                          % (self.frames, (self.trk_hist[-1] - self.trk_hist[0]) * self.bw / self.N),
                          flush=True)
                self.reset()
                return nout, True
            # waiting for the header decoder; give up after ~64 symbols of input
            if self.buf_start + len(self.buf) - self.pos > 64 * self.L:
                self.reset()
                return nout, True
            return nout, False
        if nout + self.N > len(out) or not self.have(self.pos, self.L + self.os):
            return nout, False
        x = self.window(self.pos)
        if self.track:
            # predict: apply the frequency expected for this symbol, then measure how far
            # its de-chirped peak still sits from the bin centre and update the loop
            x = x * np.exp(-2j * np.pi * self.trk_f * self.kk / self.N)
            Y = np.fft.fft(x * self.down)
            P = Y.real ** 2 + Y.imag ** 2
            m = int(np.argmax(P))
            if P[m] > self.trk_gate * np.mean(P):
                a, b, c = Y[(m - 1) % self.N], Y[m], Y[(m + 1) % self.N]
                den = 2 * b - a - c
                delta = float(np.real((a - c) / den)) if abs(den) > 0 else 0.0
                delta = float(np.clip(delta, -0.5, 0.5))   # residual offset [bins]
                self.trk_r += self.trk_g2 * delta
                self.trk_f += self.trk_g1 * delta
            self.trk_hist.append(self.trk_f)
            self.trk_f += self.trk_r
        out[nout:nout + self.N] = x.astype(np.complex64)
        if self.n_out == 0:
            meta = pmt.make_dict()
            for k, v in self.info.items():
                meta = pmt.dict_add(meta, pmt.intern(k), pmt.to_pmt(v))
            self.add_item_tag(0, self.nitems_written(0) + nout,
                              pmt.intern('frame_start'), meta)
        self.n_out += 1
        self.pos += self.L_eff
        return nout + self.N, True

    # ------------------------------------------------------------ scheduler
    def forecast(self, noutput_items, ninputs):
        return [1] * ninputs

    def general_work(self, input_items, output_items):
        inp = input_items[0]
        n_in = len(inp)
        if n_in:
            self.buf = np.concatenate((self.buf, inp))
            self.consume(0, n_in)
        out = output_items[0]
        nout = 0
        while True:
            if self.state == 'DETECT':
                prog = self.step_detect()
            elif self.state == 'SFD':
                prog = self.step_sfd()
            else:
                nout, prog = self.step_output(out, nout)
            if not prog:
                break
        # keep 2 symbols of history behind the read pointer (alignment looks back)
        keep_from = int(self.pos) - 2 * self.L - self.os
        if keep_from > self.buf_start:
            cut = keep_from - self.buf_start
            self.buf = self.buf[cut:]
            self.buf_start = keep_from
        return nout
