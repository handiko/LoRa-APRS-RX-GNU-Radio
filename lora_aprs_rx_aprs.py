"""
LoRa APRS Parser -- message-only Embedded Python Block (EPB #5).

Input  'pdu' : PDU (meta, u8vector) from the LoRa PHY Decoder's 'payload' port.
Output 'aprs': PDU (meta, u8vector) holding just the TNC2 text line
               ("SRC>DST,PATH:info") of every good packet, ready for a stock
               Socket PDU / Message Debug / File-logging chain.

LoRa APRS framing (lora-aprs / CA2RXU / OE5BPA firmware family):
    3 header bytes  '<' 0xFF 0x01   then plain TNC2 text, no AX.25, no FCS.
    (The LoRa CRC-16 is the only integrity check.)

Decodes the APRS formats LoRa trackers and iGates actually send:
    ! = / @      position, uncompressed or base-91 compressed (+course/speed/alt)
    ` '          Mic-E (lat/lon from the destination field, speed/course, alt)
    ;  )         objects and items
    :            messages (incl. ack/rej, message number)
    >            status
    T#           telemetry (printed raw)
    }            third-party (inner packet parsed recursively)
Anything else is printed as raw info text.
"""
import time
import urllib.parse
import pmt
from gnuradio import gr

SYMBOLS = {
    '/>': 'car', '/<': 'motorcycle', '/[': 'jogger', '/b': 'bicycle', '/k': 'truck',
    '/v': 'van', '/u': 'truck (18-wheeler)', '/j': 'jeep', '/R': 'RV', '/Y': 'yacht',
    '/s': 'ship', '/f': 'fire truck', '/a': 'ambulance', '/O': 'balloon', '/^': 'aircraft',
    '/X': 'helicopter', '/-': 'house', '/#': 'digipeater', '/&': 'gateway', '/_': 'weather station',
    '/r': 'repeater', '/y': 'house + yagi', '/`': 'dish antenna', '/n': 'node',
    '\\&': 'gateway (overlay)', 'L&': 'LoRa iGate', 'L#': 'LoRa digipeater',
    'I&': 'iGate', '\\#': 'digipeater (overlay)', '/l': 'laptop', '/$': 'phone',
}

MICE_MSG = {7: 'Off Duty', 6: 'En Route', 5: 'In Service', 4: 'Returning',
            3: 'Committed', 2: 'Special', 1: 'Priority', 0: 'EMERGENCY'}


def b91(s):
    v = 0
    for c in s:
        v = v * 91 + (ord(c) - 33)
    return v


class blk(gr.basic_block):
    """LoRa APRS Parser: strips the '<\\xff\\x01' header and decodes TNC2 APRS"""

    def __init__(self, show_bad_crc=False, log_path=''):
        gr.basic_block.__init__(self, name='LoRa APRS Parser', in_sig=None, out_sig=None)
        self.show_bad_crc = bool(show_bad_crc)
        self.log_path = str(log_path)
        self.message_port_register_in(pmt.intern('pdu'))
        self.set_msg_handler(pmt.intern('pdu'), self.handle)
        self.message_port_register_out(pmt.intern('aprs'))
        self.count = 0

    # ================================================================ entry
    def handle(self, msg):
        try:
            meta = pmt.to_python(pmt.car(msg)) or {}
            data = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        except Exception as e:                      # not a PDU
            print('[APRS] bad PDU: %s' % e, flush=True)
            return
        crc = meta.get('crc', 'none')
        if crc == 'FAIL' and not self.show_bad_crc:
            print('[APRS] frame dropped: LoRa CRC failed (%d bytes)' % len(data), flush=True)
            return

        if data[:3] == b'<\xff\x01':
            text, framing = data[3:], 'LoRa-APRS'
        else:
            text, framing = data, 'no <FF01 header'
        line = text.decode('utf-8', errors='replace').rstrip('\r\n\x00')

        self.count += 1
        stamp = time.strftime('%H:%M:%S')
        print('\n' + '=' * 72, flush=True)
        print('[APRS #%d %s] %s   (LoRa CRC %s, SNR %.1f dB, CFO %+.0f Hz, %s)'
              % (self.count, stamp, line, crc, meta.get('snr_db', 0.0),
                 meta.get('cfo_hz', 0.0), framing), flush=True)
        for row in self.decode_tnc2(line):
            print('    ' + row, flush=True)
        print('=' * 72, flush=True)

        raw = line.encode('utf-8', errors='replace')
        self.message_port_pub(pmt.intern('aprs'),
                              pmt.cons(pmt.car(msg), pmt.init_u8vector(len(raw), list(raw))))
        if self.log_path:
            try:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write('%s\t%s\t%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), crc, line))
            except OSError as e:
                print('[APRS] log write failed: %s' % e, flush=True)

    # ================================================================ TNC2
    def decode_tnc2(self, line, depth=0):
        rows = []
        if '>' not in line or ':' not in line:
            return ['(not a TNC2 APRS line)']
        head, info = line.split(':', 1)
        src, rest = head.split('>', 1)
        parts = rest.split(',')
        dst, path = parts[0], parts[1:]
        rows.append('From %s  to %s%s' % (src, dst, ('  via ' + ','.join(path)) if path else ''))
        if not info:
            return rows + ['(empty info field)']
        try:
            rows += self.decode_info(info, dst, depth)
        except Exception as e:                       # malformed packet: show what we can
            rows.append('Info (unparsed, %s): %s' % (e.__class__.__name__, info))
        if depth == 0:
            call = urllib.parse.quote('a/' + src, safe='')
            rows.append('Map:  https://aprs.fi/#!call=%s' % call)
        return rows

    def decode_info(self, info, dst, depth):
        t = info[0]
        if t in '!=':
            return self.position(info[1:], 'Position%s' % (' (messaging)' if t == '=' else ''))
        if t in '/@':
            return ['Time %s' % self.timestamp(info[1:8])] + \
                self.position(info[8:], 'Position%s' % (' (messaging)' if t == '@' else ''))
        if t in '`\'\x1c\x1d':
            return self.mic_e(info, dst)
        if t == ';':
            name, live = info[1:10].rstrip(), info[10]
            rows = ['Object "%s" (%s)  time %s' % (name, 'live' if live == '*' else 'killed',
                                                  self.timestamp(info[11:18]))]
            return rows + self.position(info[18:], 'Object position')
        if t == ')':
            k = min([i for i in (info.find('!', 1), info.find('_', 1)) if i > 0] or [len(info)])
            return ['Item "%s"' % info[1:k]] + self.position(info[k + 1:], 'Item position')
        if t == ':':
            to, body = info[1:10].strip(), info[11:]
            num = ''
            if '{' in body:
                body, num = body.rsplit('{', 1)
            if body.startswith('ack') or body.startswith('rej'):
                return ['%s for message %s to %s' % (body[:3].upper(), body[3:], to)]
            return ['Message to %s: "%s"%s' % (to, body, ('  [msg no %s]' % num) if num else '')]
        if t == '>':
            return ['Status: %s' % info[1:]]
        if t == 'T':
            return ['Telemetry: %s' % info[1:]]
        if t == '}' and depth < 3:
            return ['Third-party packet:'] + ['  ' + r for r in self.decode_tnc2(info[1:], depth + 1)]
        return ['Info: %s' % info]

    # ================================================================ helpers
    @staticmethod
    def timestamp(s):
        if len(s) < 7:
            return s
        if s[6] in 'z/':
            return 'day %s %s:%s %s' % (s[0:2], s[2:4], s[4:6], 'UTC' if s[6] == 'z' else 'local')
        if s[6] == 'h':
            return '%s:%s:%s UTC' % (s[0:2], s[2:4], s[4:6])
        return s

    @staticmethod
    def sym(table, code):
        key = table + code
        name = SYMBOLS.get(key) or SYMBOLS.get('\\' + code if table not in '/\\' else key, '')
        return 'symbol %s%s%s' % (table, code, ('  (%s)' % name) if name else '')

    @staticmethod
    def latlon_str(lat, lon):
        return '%+.5f, %+.5f   (%s %s)' % (
            lat, lon,
            "%d°%06.3f'%s" % (int(abs(lat)), (abs(lat) % 1) * 60, 'N' if lat >= 0 else 'S'),
            "%d°%06.3f'%s" % (int(abs(lon)), (abs(lon) % 1) * 60, 'E' if lon >= 0 else 'W'))

    def position(self, s, label):
        if not s:
            return ['%s: (missing)' % label]
        if s[0].isdigit() or s[0] == ' ':
            return self.pos_uncompressed(s, label)
        return self.pos_compressed(s, label)

    def pos_uncompressed(self, s, label):
        lat_s, table, lon_s, code, comment = s[0:8], s[8], s[9:18], s[18], s[19:]
        lat = (int(lat_s[0:2]) + float(lat_s[2:7].replace(' ', '0')) / 60.0) * \
            (1 if lat_s[7] in 'Nn' else -1)
        lon = (int(lon_s[0:3]) + float(lon_s[3:8].replace(' ', '0')) / 60.0) * \
            (1 if lon_s[8] in 'Ee' else -1)
        rows = ['%s: %s' % (label, self.latlon_str(lat, lon)),
                '%s  (uncompressed)' % self.sym(table, code)]
        motion = []
        if len(comment) >= 7 and comment[3] == '/' and comment[:3].isdigit() and comment[4:7].isdigit():
            motion.append('course %d°  speed %d km/h' % (int(comment[:3]),
                                                        round(int(comment[4:7]) * 1.852)))
            comment = comment[7:]
        i = comment.find('/A=')
        if i >= 0 and comment[i + 3:i + 9].lstrip('-').isdigit():
            motion.append('alt %d m' % round(int(comment[i + 3:i + 9]) * 0.3048))
            comment = comment[:i] + comment[i + 9:]
        if motion:
            rows.append('  '.join(motion))
        if comment.strip():
            rows.append('Comment: %s' % comment.strip())
        return rows

    def pos_compressed(self, s, label):
        table, lat4, lon4, code, cs, ctype, comment = s[0], s[1:5], s[5:9], s[9], s[10:12], s[12], s[13:]
        lat = 90.0 - b91(lat4) / 380926.0
        lon = -180.0 + b91(lon4) / 190463.0
        rows = ['%s: %s' % (label, self.latlon_str(lat, lon)),
                '%s  (compressed)' % self.sym(table, code)]
        motion = []
        if cs[0] != ' ':
            c, sp = ord(cs[0]) - 33, ord(cs[1]) - 33
            T = ord(ctype) - 33
            if (T & 0x18) == 0x10:                                  # GGA source -> altitude
                motion.append('alt %d m' % round((1.002 ** (c * 91 + sp)) * 0.3048))
            elif 0 <= c <= 89:
                motion.append('course %d°  speed %d km/h'
                              % (c * 4, round((1.08 ** sp - 1) * 1.852)))
            elif c == 90:
                motion.append('radio range %.0f km' % (2 * 1.08 ** sp * 1.609))
        i = comment.find('/A=')
        if i >= 0 and comment[i + 3:i + 9].lstrip('-').isdigit():
            motion.append('alt %d m' % round(int(comment[i + 3:i + 9]) * 0.3048))
            comment = comment[:i] + comment[i + 9:]
        if motion:
            rows.append('  '.join(motion))
        if comment.strip():
            rows.append('Comment: %s' % comment.strip())
        return rows

    def mic_e(self, info, dst):
        d = dst.split('-')[0]
        if len(d) < 6 or len(info) < 9:
            return ['Mic-E (truncated): %s' % info]
        digits, bits, custom = [], [], False
        for ch in d[:6]:
            if ch.isdigit():
                digits.append(int(ch)); bits.append(0)
            elif 'A' <= ch <= 'J':
                digits.append(ord(ch) - 65); bits.append(1); custom = True
            elif 'P' <= ch <= 'Y':
                digits.append(ord(ch) - 80); bits.append(1)
            elif ch in 'KLZ':
                digits.append(0); bits.append(0 if ch == 'L' else 1); custom |= ch == 'K'
            else:
                return ['Mic-E (bad destination %s): %s' % (dst, info)]
        lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0) / 60.0
        north = d[3] >= 'P'
        lon_off = 100 if d[4] >= 'P' else 0
        west = d[5] >= 'P'
        lat = lat if north else -lat

        deg = ord(info[1]) - 28 + lon_off
        if 180 <= deg <= 189:
            deg -= 80
        elif 190 <= deg <= 199:
            deg -= 190
        mins = ord(info[2]) - 28
        if mins >= 60:
            mins -= 60
        hund = ord(info[3]) - 28
        lon = deg + (mins + hund / 100.0) / 60.0
        lon = -lon if west else lon

        sp, dc, se = ord(info[4]) - 28, ord(info[5]) - 28, ord(info[6]) - 28
        speed = sp * 10 + dc // 10
        if speed >= 800:
            speed -= 800
        course = (dc % 10) * 100 + se
        if course >= 400:
            course -= 400
        code, table, comment = info[7], info[8], info[9:]

        msg_id = bits[0] * 4 + bits[1] * 2 + bits[2]
        status = ('Custom-%d' % msg_id) if custom else MICE_MSG.get(msg_id, '?')
        rows = ['Mic-E position: %s' % self.latlon_str(lat, lon),
                '%s   status: %s' % (self.sym(table, code), status)]
        motion = ['course %d°  speed %d km/h' % (course, round(speed * 1.852))]
        # optional altitude: 3 base-91 chars followed by '}' (metres + 10000)
        for k in (0, 1):
            if len(comment) >= k + 4 and comment[k + 3] == '}':
                motion.append('alt %d m' % (b91(comment[k:k + 3]) - 10000))
                comment = comment[:k] + comment[k + 4:]
                break
        rows.append('  '.join(motion))
        comment = comment.lstrip('>]`\'').rstrip()
        if comment:
            rows.append('Comment: %s' % comment)
        return rows
