#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: LoRa APRS RX 432.150 MHz (SF12 / 125 kHz / CR 4/5)
# Description: LoRa APRS receiver 432.150 MHz SF12/BW125/CR4-5: RTL-SDR -> own LoRa PHY (stock de-chirp/FFT + 3 EPBs) -> APRS decode
# GNU Radio version: 3.10.10.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import blocks
from gnuradio import fft
from gnuradio.fft import window
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import soapy
import lora_aprs_rx_aprs as aprs  # embedded python block
import lora_aprs_rx_lora_dec as lora_dec  # embedded python block
import lora_aprs_rx_lora_sync as lora_sync  # embedded python block
import numpy as np
import sip



class lora_aprs_rx(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "LoRa APRS RX 432.150 MHz (SF12 / 125 kHz / CR 4/5)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("LoRa APRS RX 432.150 MHz (SF12 / 125 kHz / CR 4/5)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "lora_aprs_rx")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.sf = sf = 12
        self.os_factor = os_factor = 8
        self.bw = bw = 125e3
        self.samp_rate = samp_rate = bw*os_factor
        self.rx_os = rx_os = 4
        self.N = N = 2**sf
        self.t_sym = t_sym = (2**sf)/bw
        self.sync_word = sync_word = 0x12
        self.rx_taps = rx_taps = firdes.low_pass(1.0, samp_rate, 80e3, 20e3)
        self.rx_rate = rx_rate = bw*rx_os
        self.rtl_ppm = rtl_ppm = 35
        self.rtl_gain = rtl_gain = 35
        self.rec_file = rec_file = "lora_aprs_432150_1Msps.cf32"
        self.preamble_len = preamble_len = 8
        self.ldro = ldro = 1 if (2**sf)/bw > 16e-3 else 0
        self.if_offset = if_offset = 200e3
        self.downchirp = downchirp = list(np.exp(-2j*np.pi*(np.arange(N)**2/(2.0*N) - np.arange(N)/2.0)))
        self.cr = cr = 1
        self.center_freq = center_freq = 432.150e6

        ##################################################
        # Blocks
        ##################################################

        self._rtl_gain_range = qtgui.Range(0, 49.6, 0.1, 35, 200)
        self._rtl_gain_win = qtgui.RangeWidget(self._rtl_gain_range, self.set_rtl_gain, "RTL-SDR gain [dB]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._rtl_gain_win, 0, 0, 1, 2)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.xlating = filter.freq_xlating_fir_filter_ccc((os_factor//rx_os), rx_taps, if_offset, samp_rate)
        self.waterfall = qtgui.waterfall_sink_c(
            2048, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            (center_freq - if_offset), #fc
            samp_rate, #bw
            "RTL-SDR input: LoRa APRS burst at +" + str(int(if_offset/1e3)) + " kHz (" + str(center_freq/1e6) + " MHz)", #name
            1, #number of inputs
            None # parent
        )
        self.waterfall.set_update_time(0.05)
        self.waterfall.enable_grid(False)
        self.waterfall.enable_axis_labels(True)



        labels = ['', '', '', '', '',
                  '', '', '', '', '']
        colors = [0, 0, 0, 0, 0,
                  0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
                  1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.waterfall.set_line_label(i, "Data {0}".format(i))
            else:
                self.waterfall.set_line_label(i, labels[i])
            self.waterfall.set_color_map(i, colors[i])
            self.waterfall.set_line_alpha(i, alphas[i])

        self.waterfall.set_intensity_range(-110, -40)

        self._waterfall_win = sip.wrapinstance(self.waterfall.qwidget(), Qt.QWidget)

        self.top_grid_layout.addWidget(self._waterfall_win, 1, 0, 1, 2)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.vec_sync = qtgui.vector_sink_f(
            N,
            0,
            1,
            "FFT bin = symbol value",
            "|FFT|",
            "SYNCHRONISED de-chirped spectrum of the latest symbol",
            1, # Number of inputs
            None # parent
        )
        self.vec_sync.set_update_time(0.10)
        self.vec_sync.set_y_axis(0, 1)
        self.vec_sync.enable_autoscale(True)
        self.vec_sync.enable_grid(True)
        self.vec_sync.set_x_axis_units("")
        self.vec_sync.set_y_axis_units("")
        self.vec_sync.set_ref_level(0)


        labels = ['symbol', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.vec_sync.set_line_label(i, "Data {0}".format(i))
            else:
                self.vec_sync.set_line_label(i, labels[i])
            self.vec_sync.set_line_width(i, widths[i])
            self.vec_sync.set_line_color(i, colors[i])
            self.vec_sync.set_line_alpha(i, alphas[i])

        self._vec_sync_win = sip.wrapinstance(self.vec_sync.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._vec_sync_win, 2, 1, 1, 1)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.sync_s2v = blocks.stream_to_vector(gr.sizeof_gr_complex*1, N)
        self.rtlsdr_source = None
        dev = 'driver=rtlsdr'
        stream_args = 'bufflen=16384'
        tune_args = ['']
        settings = ['']

        def _set_rtlsdr_source_gain_mode(channel, agc):
            self.rtlsdr_source.set_gain_mode(channel, agc)
            if not agc:
                  self.rtlsdr_source.set_gain(channel, self._rtlsdr_source_gain_value)
        self.set_rtlsdr_source_gain_mode = _set_rtlsdr_source_gain_mode

        def _set_rtlsdr_source_gain(channel, name, gain):
            self._rtlsdr_source_gain_value = gain
            if not self.rtlsdr_source.get_gain_mode(channel):
                self.rtlsdr_source.set_gain(channel, gain)
        self.set_rtlsdr_source_gain = _set_rtlsdr_source_gain

        def _set_rtlsdr_source_bias(bias):
            if 'biastee' in self._rtlsdr_source_setting_keys:
                self.rtlsdr_source.write_setting('biastee', bias)
        self.set_rtlsdr_source_bias = _set_rtlsdr_source_bias

        self.rtlsdr_source = soapy.source(dev, "fc32", 1, "",
                                  stream_args, tune_args, settings)

        self._rtlsdr_source_setting_keys = [a.key for a in self.rtlsdr_source.get_setting_info()]

        self.rtlsdr_source.set_sample_rate(0, samp_rate)
        self.rtlsdr_source.set_frequency(0, (center_freq - if_offset))
        self.rtlsdr_source.set_frequency_correction(0, rtl_ppm)
        self.set_rtlsdr_source_bias(bool(False))
        self._rtlsdr_source_gain_value = rtl_gain
        self.set_rtlsdr_source_gain_mode(0, bool(False))
        self.set_rtlsdr_source_gain(0, 'TUNER', rtl_gain)
        self.mon_time = qtgui.time_sink_f(
            160, #size
            1.0/t_sym, #samp_rate
            "FREE-RUNNING de-chirp: argmax bin per 32.8 ms window", #name
            1, #number of inputs
            None # parent
        )
        self.mon_time.set_update_time(0.10)
        self.mon_time.set_y_axis(-100, N*1.05)

        self.mon_time.set_y_label("FFT bin", "")

        self.mon_time.enable_tags(False)
        self.mon_time.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.mon_time.enable_autoscale(False)
        self.mon_time.enable_grid(True)
        self.mon_time.enable_axis_labels(True)
        self.mon_time.enable_control_panel(False)
        self.mon_time.enable_stem_plot(False)


        labels = ['peak bin', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [0, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [0, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.mon_time.set_line_label(i, "Data {0}".format(i))
            else:
                self.mon_time.set_line_label(i, labels[i])
            self.mon_time.set_line_width(i, widths[i])
            self.mon_time.set_line_color(i, colors[i])
            self.mon_time.set_line_style(i, styles[i])
            self.mon_time.set_line_marker(i, markers[i])
            self.mon_time.set_line_alpha(i, alphas[i])

        self._mon_time_win = sip.wrapinstance(self.mon_time.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._mon_time_win, 2, 0, 1, 1)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.mon_s2v = blocks.stream_to_vector(gr.sizeof_gr_complex*1, N)
        self.mon_s2f = blocks.short_to_float(1, 1)
        self.mon_ref = blocks.vector_source_c(downchirp, True, 1, [])
        self.mon_null = blocks.null_sink(gr.sizeof_short*1)
        self.mon_mult = blocks.multiply_vcc(1)
        self.mon_mag = blocks.complex_to_mag(N)
        self.mon_keep = blocks.keep_one_in_n(gr.sizeof_gr_complex*1, rx_os)
        self.mon_fft = fft.fft_vcc(N, True, window.rectangular(N), False, 1)
        self.mon_argmax = blocks.argmax_fs(N)
        self.mag_sync = blocks.complex_to_mag(N)
        self.mag2 = blocks.complex_to_mag_squared(N)
        self.lora_sync = lora_sync.blk(sf=sf, bw=bw, samp_rate=rx_rate, sync_word=sync_word, preamble_len=preamble_len, center_freq=center_freq, sro_comp=True, track=True, verbose=True)
        self.lora_dec = lora_dec.blk(sf=sf, ldro=ldro, verbose=True)
        self.fft_sync = fft.fft_vcc(N, True, window.rectangular(N), False, 1)
        self.dechirp = blocks.multiply_const_vcc(downchirp)
        self.argmax_null = blocks.null_sink(gr.sizeof_short*1)
        self.argmax = blocks.argmax_fs(N)
        self.aprs = aprs.blk(show_bad_crc=False, log_path="")


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.lora_dec, 'payload'), (self.aprs, 'pdu'))
        self.msg_connect((self.lora_dec, 'frame_info'), (self.lora_sync, 'frame_info'))
        self.connect((self.argmax, 1), (self.argmax_null, 0))
        self.connect((self.argmax, 0), (self.lora_dec, 0))
        self.connect((self.dechirp, 0), (self.fft_sync, 0))
        self.connect((self.fft_sync, 0), (self.mag2, 0))
        self.connect((self.fft_sync, 0), (self.mag_sync, 0))
        self.connect((self.lora_sync, 0), (self.sync_s2v, 0))
        self.connect((self.mag2, 0), (self.argmax, 0))
        self.connect((self.mag_sync, 0), (self.vec_sync, 0))
        self.connect((self.mon_argmax, 1), (self.mon_null, 0))
        self.connect((self.mon_argmax, 0), (self.mon_s2f, 0))
        self.connect((self.mon_fft, 0), (self.mon_mag, 0))
        self.connect((self.mon_keep, 0), (self.mon_mult, 0))
        self.connect((self.mon_mag, 0), (self.mon_argmax, 0))
        self.connect((self.mon_mult, 0), (self.mon_s2v, 0))
        self.connect((self.mon_ref, 0), (self.mon_mult, 1))
        self.connect((self.mon_s2f, 0), (self.mon_time, 0))
        self.connect((self.mon_s2v, 0), (self.mon_fft, 0))
        self.connect((self.rtlsdr_source, 0), (self.waterfall, 0))
        self.connect((self.rtlsdr_source, 0), (self.xlating, 0))
        self.connect((self.sync_s2v, 0), (self.dechirp, 0))
        self.connect((self.xlating, 0), (self.lora_sync, 0))
        self.connect((self.xlating, 0), (self.mon_keep, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "lora_aprs_rx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sf(self):
        return self.sf

    def set_sf(self, sf):
        self.sf = sf
        self.set_N(2**self.sf)
        self.set_ldro(1 if (2**self.sf)/self.bw > 16e-3 else 0)
        self.set_t_sym((2**self.sf)/self.bw)
        self.lora_dec.sf = self.sf
        self.lora_sync.sf = self.sf

    def get_os_factor(self):
        return self.os_factor

    def set_os_factor(self, os_factor):
        self.os_factor = os_factor
        self.set_samp_rate(self.bw*self.os_factor)

    def get_bw(self):
        return self.bw

    def set_bw(self, bw):
        self.bw = bw
        self.set_ldro(1 if (2**self.sf)/self.bw > 16e-3 else 0)
        self.set_rx_rate(self.bw*self.rx_os)
        self.set_samp_rate(self.bw*self.os_factor)
        self.set_t_sym((2**self.sf)/self.bw)
        self.lora_sync.bw = self.bw

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_rx_taps(firdes.low_pass(1.0, self.samp_rate, 80e3, 20e3))
        self.rtlsdr_source.set_sample_rate(0, self.samp_rate)
        self.waterfall.set_frequency_range((self.center_freq - self.if_offset), self.samp_rate)

    def get_rx_os(self):
        return self.rx_os

    def set_rx_os(self, rx_os):
        self.rx_os = rx_os
        self.set_rx_rate(self.bw*self.rx_os)
        self.mon_keep.set_n(self.rx_os)

    def get_N(self):
        return self.N

    def set_N(self, N):
        self.N = N
        self.set_downchirp(list(np.exp(-2j*np.pi*(np.arange(self.N)**2/(2.0*self.N) - np.arange(self.N)/2.0))))
        self.mon_time.set_y_axis(-100, self.N*1.05)

    def get_t_sym(self):
        return self.t_sym

    def set_t_sym(self, t_sym):
        self.t_sym = t_sym
        self.mon_time.set_samp_rate(1.0/self.t_sym)

    def get_sync_word(self):
        return self.sync_word

    def set_sync_word(self, sync_word):
        self.sync_word = sync_word

    def get_rx_taps(self):
        return self.rx_taps

    def set_rx_taps(self, rx_taps):
        self.rx_taps = rx_taps
        self.xlating.set_taps(self.rx_taps)

    def get_rx_rate(self):
        return self.rx_rate

    def set_rx_rate(self, rx_rate):
        self.rx_rate = rx_rate

    def get_rtl_ppm(self):
        return self.rtl_ppm

    def set_rtl_ppm(self, rtl_ppm):
        self.rtl_ppm = rtl_ppm
        self.rtlsdr_source.set_frequency_correction(0, self.rtl_ppm)

    def get_rtl_gain(self):
        return self.rtl_gain

    def set_rtl_gain(self, rtl_gain):
        self.rtl_gain = rtl_gain
        self.set_rtlsdr_source_gain(0, 'TUNER', self.rtl_gain)

    def get_rec_file(self):
        return self.rec_file

    def set_rec_file(self, rec_file):
        self.rec_file = rec_file

    def get_preamble_len(self):
        return self.preamble_len

    def set_preamble_len(self, preamble_len):
        self.preamble_len = preamble_len
        self.lora_sync.preamble_len = self.preamble_len

    def get_ldro(self):
        return self.ldro

    def set_ldro(self, ldro):
        self.ldro = ldro
        self.lora_dec.ldro = self.ldro

    def get_if_offset(self):
        return self.if_offset

    def set_if_offset(self, if_offset):
        self.if_offset = if_offset
        self.rtlsdr_source.set_frequency(0, (self.center_freq - self.if_offset))
        self.waterfall.set_frequency_range((self.center_freq - self.if_offset), self.samp_rate)
        self.xlating.set_center_freq(self.if_offset)

    def get_downchirp(self):
        return self.downchirp

    def set_downchirp(self, downchirp):
        self.downchirp = downchirp
        self.dechirp.set_k(self.downchirp)
        self.mon_ref.set_data(self.downchirp, [])

    def get_cr(self):
        return self.cr

    def set_cr(self, cr):
        self.cr = cr

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.rtlsdr_source.set_frequency(0, (self.center_freq - self.if_offset))
        self.waterfall.set_frequency_range((self.center_freq - self.if_offset), self.samp_rate)




def main(top_block_cls=lora_aprs_rx, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
