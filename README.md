# My LoRa APRS Receiver in GNU Radio, and the 45 Hz That Nearly Beat It

**A plain-English write-up**

**The flowgraph:** `lora_aprs_rx.grc` (GNU Radio Companion 3.10). It receives LoRa APRS on 432.150 MHz (SF12, 125 kHz, CR 4/5) with an RTL-SDR dongle and prints the decoded APRS packets.

**Who I wrote this for:** fellow hams and RF people who are at home with mixers, filters, crystals and APRS, but don't do signal-processing maths for a living. I've kept the maths out. If you want it, it's in the companion documents.

**Companion documents:**

- `lora_aprs_rx_explained.md`: the same receiver, technical, with all the test tables.
- `lora_rx_plain_english.md`: my earlier LoRa receiver write-up. It goes deeper into how a LoRa receiver separates timing errors from tuning errors; here I only summarise that part and spend my time on what's new.

**Diagrams:** the eleven `lora_aprs_pe_*.svg` files need to sit in the same folder as this document. VS Code, Obsidian, GitHub and most browsers then show them inline. Every chart of measured data comes from a real Gqrx recording of my own tracker, YB1SDL-9. None of them are sketched.

---

## Contents

**Part 1: How my receiver works**

1. [Why I built it](#1-why-i-built-it)
2. [What LoRa APRS is](#2-what-lora-aprs-is)
3. [The receiver at a glance](#3-the-receiver-at-a-glance)
4. [What my tracker actually sends](#4-what-my-tracker-actually-sends)
5. [Step 1: Tuning in](#5-step-1-tuning-in)
6. [Step 2: The core trick, un-sweeping a chirp](#6-step-2-the-core-trick-un-sweeping-a-chirp)
7. [Step 3: Two errors that look the same](#7-step-3-two-errors-that-look-the-same)
8. [Step 4: Locking on to a frame](#8-step-4-locking-on-to-a-frame)
9. [Step 5: Reading each symbol with stock blocks](#9-step-5-reading-each-symbol-with-stock-blocks)
10. [Step 6: From symbol values to bytes](#10-step-6-from-symbol-values-to-bytes)
11. [Step 7: From bytes to an APRS position](#11-step-7-from-bytes-to-an-aprs-position)
12. [What I see on the screen](#12-what-i-see-on-the-screen)

**Part 2: The night it decoded one frame in ten**

13. [The symptom](#13-the-symptom)
14. [My three suspects](#14-my-three-suspects)
15. [What the recording showed me](#15-what-the-recording-showed-me)
16. [The wrong turn I took](#16-the-wrong-turn-i-took)
17. [The key: groups of four, and a one-sided safety margin](#17-the-key-groups-of-four-and-a-one-sided-safety-margin)
18. [The fix: following the frequency through the payload](#18-the-fix-following-the-frequency-through-the-payload)
19. [How well it works now](#19-how-well-it-works-now)
20. [The whole hunt in one picture, and what I took away from it](#20-the-whole-hunt-in-one-picture-and-what-i-took-away-from-it)
21. [What my receiver still can't do](#21-what-my-receiver-still-cant-do)
22. [Glossary](#22-glossary)

---

# Part 1: How my receiver works

## 1. Why I built it

I run a LoRa APRS tracker, YB1SDL-9, on 432.150 MHz. Plenty of iGates around me already decode it with proper LoRa chips. I wanted to receive it myself with nothing more than an RTL-SDR dongle, a Windows laptop and stock GNU Radio: no LoRa out-of-tree modules, and no hardware LoRa receiver.

I set myself one rule for the flowgraph: **if GNU Radio has a stock block for a job, use it. Write an Embedded Python Block only for what GNU Radio genuinely can't do.** I wanted to be able to open the flowgraph and *see* the radio working, not hide it all inside one big Python box.

In short, my receiver does seven things:

1. **Tunes in.** The dongle listens 200 kHz below the channel; a filter brings the channel to the centre and cuts away everything else.
2. **Finds a frame.** It watches for the preamble, the eight identical chirps that start every LoRa frame.
3. **Measures the errors.** It works out how far off-tune the signal is and exactly when each chirp starts.
4. **Lines up and follows.** It cuts the signal into one clean slice per chirp, and **keeps re-centring the frequency on every chirp**, because my tracker drifts while it transmits. Most of Part 2 is about that last bit.
5. **Reads each chirp's value.** Stock GNU Radio blocks "un-sweep" each slice and find its strongest frequency.
6. **Turns values into bytes.** It undoes LoRa's scrambling, shuffling and error coding, and checks the checksum.
7. **Reads the APRS text.** It decodes callsign, position, course, speed, altitude and comment, and prints them with an aprs.fi link.

The first night I put it on air, it decoded my tracker **once in ten tries**. The cause turned out to be a frequency drift of a few tens of hertz *during* each transmission, combined with a quirk of how LoRa packs its bits at SF12. I explain both in Part 2. With the fix in place, every test frame decodes, and the receiver copes with drift from −300 to +345 Hz per frame.

---

## 2. What LoRa APRS is

**LoRa** is Semtech's long-range modulation, built into cheap modules such as the SX1278 (433 MHz band) and the SX1262. It trades speed for range, and it can be received even when the signal is *below* the noise.

**APRS** is the position-reporting system we all know: `CALLSIGN>DEST,PATH:information`, normally sent as AX.25 packets at 1200 baud on VHF.

**LoRa APRS** puts the same APRS text on LoRa instead of AX.25. Each packet is:

- three marker bytes: `<`, `0xFF`, `0x01`;
- then the APRS line as plain text, in "TNC2" format (`YB1SDL-9>APRS,WIDE2-2:!0620.56S/...`);
- no AX.25 framing and no extra checksum. The LoRa chip's own CRC-16 is the only error check.

**The settings my network uses:**

| Setting | Value | What it means |
|---|---|---|
| Frequency | 432.150 MHz | Channel centre |
| Bandwidth (BW) | 125 kHz | Width of each chirp's sweep |
| Spreading factor (SF) | 12 | Each chirp has 4096 possible positions; the slowest, longest-range setting |
| Coding rate (CR) | 4/5 ("CR5") | Every 4 data bits get 1 check bit |
| Sync word | 0x12 | A network ID; LoRa APRS uses the SX127x "private network" value |
| Low data rate mode | On | Automatic at SF12/125 kHz; I come back to it in §10 and §17 |

**Why SF12 matters for everything that follows.** Each chirp lasts 32.8 ms and a whole frame about 3.5 s. That's long enough for a transmitter's frequency to change noticeably within a single frame. At faster LoRa settings this rarely shows up. At SF12, it bit me.

---

## 3. The receiver at a glance

![The whole receiver: RTL-SDR, Xlating filter, Frame Sync, stock un-sweep chain, decoder, APRS parser](lora_aprs_pe_big_picture.svg)

*Green boxes are stock GNU Radio blocks, coral boxes are my Embedded Python Blocks (EPBs), grey boxes are displays. Solid arrows carry the sample stream; dashed arrows carry messages, sent once per frame.*

| Stage | Block | What it does for me | Like… |
|---|---|---|---|
| Dongle | Soapy RTL-SDR Source | Listens to 1 MHz of spectrum around 431.950 MHz | The antenna, front end and ADC of a receiver |
| Front end | Frequency Xlating FIR Filter | Brings 432.150 MHz to the centre, filters to the channel, halves the sample rate | Mixer + IF filter |
| **EPB 1: Frame Sync** | Embedded Python | Finds each frame, measures tuning and timing, cuts one clean slice per chirp, and **follows frequency drift** | A LoRa chip's synchroniser |
| Un-sweep chain | Stream to Vector, Multiply Const, FFT, Complex to Mag², Argmax | Turns each slice into one number: the chirp's value | A LoRa chip's demodulator |
| **EPB 2: PHY Decoder** | Embedded Python | Turns chirp values into bytes and checks the CRC | The digital back end of the LoRa chip |
| **EPB 3: APRS Parser** | Embedded Python | Reads the APRS text and prints position, course, speed, altitude and comment | The decode window of an APRS client |

**Why these three, and only these three, are Python.**

- **Frame Sync:** no stock block can decide *where to cut* a stream based on what it has just measured, or move its own cutting points as it goes.
- **PHY Decoder:** GNU Radio has no LoRa bit-level blocks.
- **APRS Parser:** GNU Radio has no APRS text parser.

The heart of LoRa demodulation, the un-sweeping and the symbol decisions, is done entirely by stock blocks. I like that: I can click on any of those blocks in GRC and see exactly what it does.

**Two message connections** (dashed) complete the picture:

- The decoder tells the Frame Sync **how long the frame is**. The length sits inside the frame's header, so the Frame Sync can't know it in advance.
- The decoder hands each decoded packet to the APRS Parser.

---

## 4. What my tracker actually sends

![Anatomy of a LoRa APRS frame at SF12: preamble, sync word, SFD, header, payload, and the payload bytes](lora_aprs_pe_frame.svg)

*Top: the frequency of my tracker's signal, symbol by symbol. The payload chirps are the real values I decoded from my recording. Middle: the frame on a time scale. Bottom: what the 85 payload bytes contain.*

### 4.1 Chirps and symbols

Every LoRa **symbol** is one chirp: a tone that sweeps steadily upward across the 125 kHz channel in 32.8 ms. The data is **where in the sweep the chirp starts**:

- symbol value 0 starts at the bottom edge;
- value 1000 starts 1000/4096 of the way up;
- when the sweep hits the top edge, it jumps to the bottom and carries on. I call that jump "the fold".

At SF12 there are 4096 possible starting points, so one chirp could in principle carry 12 bits. (In §10 I explain why at SF12 it actually carries 10.)

### 4.2 The parts of a frame

| Part | Length | Duration | What it's for |
|---|---|---|---|
| **Preamble** | 8 identical up-chirps (value 0) | 0.26 s | "Wake up, a frame is coming." My receiver detects frames by these. |
| **Sync word** | 2 chirps, values 8 and 16 | 0.07 s | Network ID (0x12). Other networks use other values and get ignored. |
| **SFD** (start-of-frame delimiter) | 2¼ *down*-chirps | 0.07 s | Marks where data begins, and lets the receiver separate timing errors from tuning errors (§7) |
| **Header** | 8 symbols | 0.26 s | Length (85 bytes), coding rate (4/5), CRC on. Heavily protected. |
| **Payload** | 85 symbols for my 85-byte packet | 2.79 s | The packet itself plus the CRC. Lightly protected. |

**About 3.45 s on air in total.** The contrast to keep in mind is between the header and the payload. The header comes early and is strongly protected; the payload is long and has hardly any protection. That contrast is exactly why my problem in Part 2 showed up as "header fine, payload broken".

---

## 5. Step 1: Tuning in

**Blocks:** `Soapy RTL-SDR Source` → `Frequency Xlating FIR Filter`.

1. **I tune the dongle to 431.950 MHz, 200 kHz below the channel.** Every RTL-SDR has a spike of junk at exactly its tuned frequency (the DC offset). Tuning off to one side keeps that spike out of the LoRa channel.
2. **It delivers 1,000,000 samples per second**, a 1 MHz wide view in which the LoRa signal sits at +200 kHz.
3. **The Xlating filter does three jobs in one block:**
   - **slides** the spectrum down by 200 kHz, so 432.150 MHz sits at 0 Hz;
   - **filters** to ±80 kHz (LoRa only needs ±62.5 kHz);
   - **keeps every second sample**, leaving 500,000 samples per second: **4 samples per "chip"**. A chip is one of the 4096 steps of a chirp; 125,000 of them pass every second.

**Why 4 samples per chip, when reading a chirp needs only 1?** Timing precision. If I take the one-per-chip samples half a chip off, the chirp reads up to 4 dB weaker and becomes ambiguous between two neighbouring values. With 4 samples per chip, the Frame Sync can pick the best of four sampling moments.

**A bonus I got for free.** My dongle's reference crystal runs at 28.8 MHz, and 15 × 28.8 MHz is **exactly 432.000 MHz**. That harmonic shows up on my waterfall as a steady vertical line 150 kHz below the channel. With the dongle at 431.950 MHz, the spur lands outside the channel filter and does no harm. You can see it in the screenshots from my first night on air.

---

## 6. Step 2: The core trick, un-sweeping a chirp

![A rising chirp mixed with a falling reference gives a steady tone, which the FFT puts into one bin](lora_aprs_pe_dechirp.svg)

*Left: the received chirp (blue, symbol 1000), my reference down-chirp (orange), and their mix (green). Right: the FFT of the mix: one tall bin at 1000.*

### 6.1 Mixing with an opposite sweep

Take the received chirp, sweeping **up**. Mix it with a locally made chirp sweeping **down** at exactly the same rate. As with any mixer, the output frequency is the sum of the two:

- the incoming frequency rises steadily;
- the reference frequency falls steadily;
- **so their sum stays constant: a plain, steady tone.**

The fold doesn't spoil this. After the fold the true sum is exactly one channel width (125 kHz) away, and at one sample per chip the sampling folds it right back onto the same tone. When I first understood this, LoRa stopped feeling like magic.

### 6.2 The tone's frequency is the symbol value

A chirp that starts 1000 steps up gives a tone 1000 steps up. Each step is 125,000 / 4096 = **30.5 Hz**. Hold on to that number: at SF12, **one step, called a "bin", is only 30.5 Hz wide.** On a 432 MHz carrier that's 0.07 ppm. It's the whole reason the problem in Part 2 exists.

### 6.3 Reading the tone: the FFT

An FFT is a frequency analyser. Applied to one chirp's worth of samples (4096 at one sample per chip), it gives me 4096 "bins". The steady tone lands in **one** bin, and the index of the strongest bin is the symbol value.

**This is also LoRa's below-the-noise trick.** The chirp's energy, spread over the full 125 kHz, gets gathered into one bin. Noise stays spread over all 4096 bins, so the signal stands out by a factor of 4096, or **36 dB**. That's why SF12 decodes signals 20 dB *below* the noise in the channel.

---

## 7. Step 3: Two errors that look the same

Before my receiver can read anything, it has to work out two things nobody tells it:

| Unknown | Why it exists | How big it was for me |
|---|---|---|
| **Timing**: where each chirp starts | My tracker can start transmitting at any moment | anything up to a whole chirp |
| **Tuning error** (CFO, carrier frequency offset) | Neither my tracker's crystal nor my dongle's is exact | a few hundred Hz: I measured −333 Hz to +334 Hz |

The catch is that **both errors move the tone in the same way**:

- a slice that starts 10 chips late makes an up-chirp's tone read 10 bins high;
- a tuning error of 10 bins (305 Hz) also makes it read 10 bins high.

From up-chirps alone, I can't tell them apart. The **SFD's down-chirps** solve this. For a down-chirp, a timing error moves the tone the *opposite* way, while a tuning error moves it the *same* way. Measure both kinds of chirp and the two errors separate. That's why every LoRa frame carries those 2¼ down-chirps. (My earlier write-up, `lora_rx_plain_english.md` §7, walks through this in detail.)

**A third, slower error: clock drift.** In the RTL-SDR, one crystal sets both the tuning and the sampling clock. In the tracker's LoRa chip, one crystal sets both the carrier and the chirp timing. So a tuning error always comes with a matching clock error: if the combined crystal error is 1 ppm, every chirp is also 1 ppm longer or shorter than expected. My receiver measures the tuning error, works out the clock error from it, and stretches its slice timetable to match.

---

## 8. Step 4: Locking on to a frame

![The five stages inside the Frame Sync: listen, fine-tune, line up, find the marker, deliver and track](lora_aprs_pe_framesync.svg)

This is the job of my first Python block, the Frame Sync. It works through five stages for every frame.

### ① Listen

The Frame Sync un-sweeps the incoming signal slice after slice, on an arbitrary timetable. On noise, the strongest bin jumps around at random. During the preamble, all eight chirps are identical, so **every slice peaks in the same bin**, even when the slices are misaligned.

**My rule: three slices in a row with their peak in the same bin (±1) means a frame has started.** Each slice's peak must also stand at least 1.5 × ln(4096) ≈ 12.5 times above the average bin, about 11 dB. My first version was stricter (four slices, about 12 dB). Loosening it raised detection in my −20 dB test from 1 frame in 5 to 4 in 5, and −20 dB is where SF12 works at its limit. False alarms stay negligible: noise passes the threshold in about 1.5 % of slices, and three in a row within ±1 bin of each other happens about once in 10¹² slices.

### ② Fine-tune

- **Fractional tuning error.** Because the preamble repeats exactly, each slice's peak is identical, except that a tuning error makes its *phase* creep by the same amount every slice. A whole-bin error makes a full turn and is invisible here, but the fraction of a bin shows up, and I correct it.
- **Best sampling moment.** I try the four possible sampling moments and keep the one that gives the sharpest peak.

### ③ Line up

The Frame Sync slides its slice timetable by the preamble's bin number. Now every preamble chirp reads 0 and the sync word reads 8 and 16. The slide has absorbed *timing plus tuning error* together; stage ④ separates them.

### ④ Find the marker

Now the Frame Sync steps forward one chirp at a time and un-sweeps each slice both ways: as an up-chirp and as a down-chirp.

- While the up-chirp reading is stronger, it's still the preamble or the sync word.
- The first slice where the down-chirp reading wins is the SFD.

Three things follow:

1. **Network ID check, allowing for clock drift.** The two readings before the SFD must be 8 and 16. Clock drift can shift both by the same amount before I see them; at 60 ppm the shift is about 2 bins. So I accept them if both are shifted equally (by up to 3.5 bins), and use that shared shift as a measurement of the drift. My first version demanded exactly 8 and 16 (±1). In testing it rejected **every** frame at ±60–70 ppm of combined crystal error; now those decode.
2. **Tuning error separated.** The SFD's reading equals twice the whole-bin tuning error, corrected for the measured drift. Added to the fraction from ②, that gives me the full tuning error, which the console prints in Hz.
3. **Exact timing.** Now that I know how much of the slide in ③ was really tuning error, I can correct the timetable. The header starts exactly 2¼ chirps after the start of the SFD.

**Range limit:** the tuning error has to be smaller than a quarter of the channel, **±31.25 kHz**. At 432 MHz that's ±72 ppm of combined crystal error. An RTL-SDR v3 or v4 (1 ppm TCXO) plus a typical tracker sits far inside that.

### ⑤ Deliver, and track

From the header onward, the Frame Sync hands out **one clean slice per chirp**: correctly timed, at the best sampling moment, re-tuned, and stretched for clock drift. It labels the first slice "start of frame" so the decoder knows where each frame begins.

It doesn't know how long the frame is, because the length is inside the header. So it delivers the 8 header chirps and waits. The decoder reads the header and sends back "this frame is 93 symbols" (8 header + 85 payload), and the Frame Sync then delivers exactly that many more. If the header fails its check, the decoder says "abort" and the Frame Sync goes back to listening.

**New since my first night on air: frequency tracking.** During stage ⑤, which lasts the whole 3 s of header and payload, the Frame Sync now measures how far each chirp's peak sits from the centre of its bin and nudges its frequency correction to match. I explain it properly in §18.

---

## 9. Step 5: Reading each symbol with stock blocks

Once the slices are aligned and corrected, reading the values takes five stock blocks:

| Block | What it does |
|---|---|
| `Stream to Vector` (4096) | Groups 4096 samples into one slice |
| `Multiply Const` (× down-chirp) | Un-sweeps: the chirp becomes a steady tone |
| `FFT` (4096 points) | Splits the slice into 4096 bins; the tone lands in one |
| `Complex to Mag²` | Strength of each bin |
| `Argmax` | Index of the strongest bin = **the symbol value** |

The `Vector display` in my GUI shows the FFT of the latest symbol: one sharp spike. With the Frame Sync doing its job, that spike sits in the middle of its bin, at full height.

---

## 10. Step 6: From symbol values to bytes

My second Python block, the PHY Decoder, runs the transmitter's processing backwards.

### 10.1 One value → bits

| Step | What happens | Why |
|---|---|---|
| Subtract 1 | Undo a +1 offset the transmitter adds | A quirk of the LoRa format |
| Gray code | Re-order the value so neighbouring values differ by one bit | A near-miss then costs one bit, not several |
| **Drop the lowest 2 bits** (SF12, low data rate mode) | Keep 10 bits instead of 12 | See below |

**Low data rate mode, in plain terms.** At SF12/125 kHz a chirp lasts 32.8 ms, long enough for small frequency and timing slips to matter. Semtech's answer is to use only 10 of the 12 bits per chirp. In effect, the 4096 positions are used in **groups of four**, and only the group matters. The header always uses this reduced mode, even at lower SFs.

How exactly the chip places a symbol *within* its group of four turned out to be the key to my problem in Part 2 (§17).

### 10.2 Bits → bytes

Next the bits are un-shuffled. LoRa interleaves them diagonally across chirps, so a single bad chirp damages one bit in many code words instead of many bits in one. Then the error code is applied:

| Part | Coding rate | What it can do |
|---|---|---|
| Header | **4/8**: 4 data bits + 4 check bits | **Corrects** one wrong bit per code word |
| Payload (CR5) | **4/5**: 4 data bits + 1 check bit | Can only **detect** an error, not fix it. My decoder doesn't use it; the CRC catches it. |

Finally the payload bytes are un-scrambled (LoRa "whitens" them with a fixed pseudo-random sequence) and the CRC-16 is checked. **With CR 4/5, a single wrong chirp anywhere in my 85 payload chirps means a failed CRC.** The header, by contrast, shrugs off a wrong bit or two.

---

## 11. Step 7: From bytes to an APRS position

![How the APRS parser splits one packet into fields, and what it prints](lora_aprs_pe_aprs_decode.svg)

My third Python block, the APRS Parser, receives each decoded packet as a message:

1. **Drops frames that failed the LoRa CRC**, and reports them as "frame dropped". Setting `show_bad_crc=True` shows them anyway.
2. **Strips the 3-byte marker** `<` `0xFF` `0x01`.
3. **Splits the TNC2 line:** source callsign, destination, digipeater path, and the information field after the colon.
4. **Decodes the information field** by its first character:

| First character | Type | Decoded into |
|---|---|---|
| `!` `=` | Position without timestamp | Latitude, longitude, symbol, course/speed, `/A=` altitude, comment |
| `/` `@` | Position with timestamp | Same, plus the time |
| (compressed form) | Base-91 position, as many LoRa trackers send | Same, plus course/speed or altitude from the compressed bytes |
| `` ` `` `'` | Mic-E | Position hidden in the destination field, speed, course, Mic-E status, altitude |
| `:` | Message | Addressee, text, message number, ack/rej |
| `>` | Status | Text |
| `;` `)` | Object / item | Name, live/killed, position |
| `T#` | Telemetry | Printed as is |
| `}` | Third-party | The inner packet, decoded again |

5. **Prints** the result with an aprs.fi link. It can also append each packet to a log file (`log_path`), and it offers the bare TNC2 line on its `aprs` message output, so a stock Socket PDU block could pass it on to another program.

I checked the parser against the independent `aprslib` Python library on uncompressed, compressed, Mic-E, message and status packets. Every field agreed.

---

## 12. What I see on the screen

| Position | Display | What I look for |
|---|---|---|
| Top | Gain slider | RF gain; ~30 dB is a good start |
| Middle | **Waterfall** of the whole 1 MHz | An SF12 frame is a 125 kHz-wide stripe at 432.150 MHz lasting 2–4 s, next to the 432.000 MHz spur. A frame only shows here above roughly −5 dB SNR, while the decoder works down to −20 dB. A blank waterfall doesn't mean nothing is being received. |
| Bottom left | **Blind de-chirp plot**: one dot per 32.8 ms slice | Preamble: a flat run of dots at one bin. Sync word: two steps. Payload and noise: scattered dots. |
| Bottom right | **Synchronised spectrum** of the latest symbol | One sharp spike: what each symbol looks like once the Frame Sync has done its work |

**The console** prints three lines per frame, for example:

```
[LoRa sync] frame 1: SNR 17.2 dB, CFO -47 Hz (-2 + 0.47 bins), sync word bins [8, 16] (drift +0.0 chips), sampling phase 0/4
[LoRa sync] frame 1: frequency tracked +44 Hz over the payload
[LoRa RX] frame 1  len 85  CR 4/5  CRC OK  CFO -47 Hz  SNR 17.2 dB
```

…followed by the decoded APRS block. "Frequency tracked" is the drift my receiver followed during that frame, and it's become my favourite number: it tells me directly how much my tracker's oscillator moved while it was transmitting.

---

# Part 2: The night it decoded one frame in ten

## 13. The symptom

Just after midnight on my first night on air, my receiver heard YB1SDL-9 ten times and decoded it once:

| Frame | Tuning error | Sync word | Header | Payload CRC | SNR |
|---|---|---|---|---|---|
| 1 | −47 Hz | 8, 16 ✓ | length 85 ✓ | **OK** | 17 dB |
| 2–10 | +206 … +334 Hz | 8, 16 ✓ | length 85 ✓ | **FAIL** | 14–25 dB |

Everything up to and including the header was perfect, every time. The SNR was excellent: 35–45 dB above where SF12 starts to struggle. Meanwhile, other LoRa APRS stations around me decoded my tracker reliably, some of them reporting SNRs as low as −18 dB.

So my tracker was fine, the signal was strong, and something in my receiver broke **only in the payload**, the long, lightly protected part of the frame.

---

## 14. My three suspects

| Suspect | Why it fitted | Why it might not |
|---|---|---|
| **My tracker's frequency drifts *during* each transmission** | My Frame Sync measured the frequency once, on the preamble, then froze it for 3 s. At 30.5 Hz per bin, a few tens of Hz of drift moves late chirps toward the neighbouring bin. The header (early, strongly protected) would survive and the payload (late, unprotected) wouldn't. The tuning error had also jumped ~280 Hz between frames 1 and 2: an oscillator on the move. | My simulations suggested the receiver tolerated ~4 bins (~120 Hz) of steady drift, which seemed like plenty |
| **Lost samples on my laptop** | A gap of even a few samples in a 3.5 s frame shifts every chirp after it: header fine, payload garbage | I saw no overflow warnings |
| **My dongle's own oscillator drifting** | Same symptom as transmitter drift | Dongle warm-up takes minutes, not seconds |

I couldn't decide between them by thinking about it. I needed a recording.

---

## 15. What the recording showed me

I recorded one frame in Gqrx: 300,000 samples per second, centred on 432.150 MHz, a 32 MB file holding a single transmission that other stations decoded. I resampled it to my receiver's rate and played it back through the flowgraph.

**It decoded.** CRC OK, YB1SDL-9, the same 85-byte packet. So the signal itself was decodable, and whatever went wrong was something that varied from frame to frame.

Then I measured the recording symbol by symbol: for each of the 93 header and payload chirps, how far its peak sits from the centre of its bin.

![The measured position of each symbol's peak, with the green 'still decodes' band and the red 'wrong bits' band](lora_aprs_pe_drift_measured.svg)

*Blue: what a receiver without tracking sees in my real frame. Dashed red: the same drift mirrored downward. The green band is where a reading still gives the right bits; the red band is where it gives wrong ones (I explain why in §17).*

**What the blue line told me:**

- **Header and the first ~25 payload chirps:** flat, about −0.13 bin. My tracker's frequency was steady.
- **From about 0.8 s into the payload:** a steady climb.
- **By the end of the frame:** +1.4 bins, about **+45 Hz** above where it started.

A flat start followed by a ramp is the classic signature of a **transmitter warming up while it transmits**. The power amplifier draws current, the module heats up, and the crystal's frequency moves. My dongle in Gqrx had been running steadily for minutes, and it wouldn't start ramping at exactly the moment my tracker's burst began. I'd put about 80 % on the drift being on my tracker's side. Most LoRa APRS trackers use modules with a plain crystal rather than a TCXO, and tens of hertz over a few seconds at 432 MHz is entirely plausible for those.

**But there was a puzzle.** From symbol 54 onward the peak sat past the half-bin line, so the receiver read those chirps **one bin too high**: 38 chirps in all. Yet the frame still passed its CRC. With CR 4/5, one wrong chirp should be fatal. Why were 38 harmless? It took me a while to find the answer, which is in §17.

---

## 16. The wrong turn I took

Before I found the real answer, I fooled myself.

**My test.** I wanted to know whether the tuning error itself mattered. So I shifted the recorded frame in software to each of the live frames' tuning errors (−47, +232, +312 Hz, …) and replayed the copies. Some of them failed. It looked like I'd reproduced the problem: "the same frame fails at certain tuning errors".

**What was wrong with it.** In real hardware, a tuning error never comes alone. The crystal that sets the frequency also sets the clock, so a frequency offset always arrives with a matching clock offset (§7). By shifting only the frequency in software, I left the clock where it was. My receiver, which works out the clock correction from the tuning error, then "corrected" for a clock drift that wasn't there. The failures came from my test, not from my receiver.

**Corrected test.** I remade the copies with the frequency *and* the clock shifted together, the way a real crystal error does it. **All twelve copies, from −600 to +400 Hz, decoded, even without tracking.**

**What I learned:** a test signal has to be physically consistent with the thing it imitates. Frequency and clock come from the same crystal, and a test that moves one without the other can produce very convincing nonsense. I'm writing this wrong turn down so I don't take it again.

So my recorded frame was fine at every tuning error. Then why had the live frames failed?

---

## 17. The key: groups of four, and a one-sided safety margin

### 17.1 The observation

I printed the exact symbol values from my recording and noticed something striking: **every one of the 93 chirps was sent on the lowest bin of its group of four.**

![Low data rate mode: each symbol sits on the lowest bin of its group of four, so readings 1–3 bins high are absorbed but 1 bin low is not](lora_aprs_pe_ldro_groups.svg)

Remember from §10 that in low data rate mode only the *group* of four matters. My decoder finds the group by subtracting 1 and dropping the lowest two bits (after Gray coding). Group g covers the four bins from 4g+1 to 4g+4. My tracker transmits on the **first** of these, 4g+1, and never on the other three.

### 17.2 What that means

- If my receiver reads a chirp **1, 2 or 3 bins too high**, the reading is still inside group g, so **the bits are still correct.**
- If it reads a chirp **just 1 bin too low**, the reading falls into group g−1: **wrong bits, failed CRC.**

So the safety margin is **lopsided**:

| Direction of drift | Margin before the bits go wrong | In Hz at SF12 |
|---|---|---|
| Upward (readings creep high) | about 3½ bins | about +110 Hz |
| Downward (readings creep low) | about ½ bin | only about **−11 to −15 Hz** |

### 17.3 Suddenly everything fitted

- **My recorded frame drifted upward** (+45 Hz). Its 38 misread chirps were all one bin *high*, inside the margin, so they were harmless. It decoded.
- **The same drift going downward** (the dashed red line in the §15 chart) would push chirps one bin *low* from about symbol 46. One such chirp is enough to fail the CRC.
- **A real LoRa chip** doesn't have this weakness, because it follows the frequency during the payload. That's why the other stations decoded every frame I sent.

### 17.4 Testing my prediction

I tested the prediction on the real recording: the original +45 Hz drift plus an extra *downward* ramp on top.

| Total drift during the payload | My receiver without tracking |
|---|---|
| +45 Hz (as recorded) | decodes |
| **−15 Hz** | **fails** |
| −30, −45, −60, −100, −300 Hz | fails |

**Fifteen hertz of downward drift, half a bin, breaks every frame.** At 432 MHz that's a change of just 0.035 ppm over three seconds, well within what a plain crystal does as it warms or cools.

**Back to my live session.** From frame 4 onward, the live tuning errors fell steadily from frame to frame: +334, +298, +293, +276, +254, +231, +206 Hz. A tracker whose frequency is falling between frames is quite likely to be falling within frames too. I didn't record those failed frames, so I can't prove it. But it's the explanation that fits everything I saw (I'd put it at about 80 %), and the fix below handles it either way.

**Why my simulations didn't catch it.** The test transmitter I used during development (the EPFL gr-lora_sdr implementation, used only as a signal generator) places symbols exactly like my tracker, on the lowest bin of each group; I checked afterwards. So the lopsided margin was there in my simulations too. They missed it for a simpler reason: **every drift I simulated went upward**, which is exactly the direction my old receiver tolerated. A simulation only tests the cases you think of putting into it.

---

## 18. The fix: following the frequency through the payload

![The frequency-tracking loop inside the Frame Sync: apply the prediction, measure the peak offset, check the peak is trustworthy, update frequency and drift rate](lora_aprs_pe_tracking_loop.svg)

### 18.1 The idea

My old Frame Sync measured the frequency once, on the preamble, and held it for 3 s. It was like a navigator who takes one fix at the start of a voyage and never looks at the coast again. The new Frame Sync takes a fix **on every chirp** and corrects its course a little each time.

### 18.2 What happens for every chirp

1. **Apply the prediction.** Before the slice leaves the Frame Sync, I nudge its frequency by the loop's current estimate of the drift ("freq").
2. **Un-sweep and FFT, internally.** This is only a *measurement*; the symbol decision is still made downstream by the stock blocks.
3. **Find the peak and its offset.** The strongest bin and its two neighbours give the peak's position to a fraction of a bin. I call the offset from the bin centre δ (delta). On clean test tones this measurement is accurate to 0.001 bin, about 0.03 Hz.
4. **Check the peak is trustworthy.** I only use peaks standing more than 2·ln(4096) ≈ 16.6 times above the average bin (12 dB). That way a noise spike or a badly faded chirp can't drag the loop off course.
5. **Update the estimate:**
   - `freq ← freq + 0.3 × δ`: move 30 % of the way toward where this chirp says the frequency is;
   - `rate ← rate + 0.02 × δ`: learn how fast the frequency is moving;
   - then, for the next chirp, `freq ← freq + rate`: assume the drift carries on at the learned rate.
6. **Send the corrected slice** on to the stock un-sweep chain.

### 18.3 Why two terms

A loop with only the first term would always trail behind a steady ramp: by the time it caught up with one chirp's drift, the frequency would have moved on again. The second term works like dead reckoning. Having seen the frequency climb by about two-thirds of a hertz per chirp for a while, it *expects* the next chirp to be that much higher and pre-corrects for it. With both terms, the loop follows a steady warm-up ramp with almost no lag.

### 18.4 Why "the next chirp", never "this chirp"

Each chirp is corrected using what the *previous* chirps taught the loop, never its own measurement. If a chirp sat exactly on a half-bin boundary, correcting it by its own measurement would be a coin toss: which side does it really belong to? By predicting from the chirps before it, the loop puts each chirp near its bin centre *before* the decision is made.

### 18.5 Why the gains are 0.3 and 0.02

I chose these and then tested in both directions:

- **Higher gains** would react faster, but let noise jiggle the frequency at low SNR.
- **Gentler gains (0.15 and 0.006):** I tried them. They gave no improvement at −20 dB and failed my heaviest drift test (+345 Hz), so I kept 0.3 / 0.02.

### 18.6 What it costs

One extra 4096-point FFT per 32.8 ms chirp, which is negligible next to everything else the flowgraph does. The whole flowgraph still runs many times faster than real time.

### 18.7 It stays inside my design rule

The tracking lives inside the Frame Sync because it needs exactly what stage ⑤ already needs: a block that decides how to cut and correct each slice based on what it has just measured. No stock block can feed a measurement back into the processing of the next slice. The symbol decisions are still made by the stock FFT and Argmax blocks.

The Frame Sync has a `track` parameter; setting it to `False` turns tracking off, which is handy for comparison.

---

## 19. How well it works now

### 19.1 On my real recording

![With tracking on, the offset of every symbol stays within ±0.2 bin; without it, the peaks slide to the bin edge and then read in the next bin](lora_aprs_pe_tracking_result.svg)

*Orange: each chirp's offset from its bin centre without tracking. It slides to the edge and wraps into the next bin. Blue: with tracking, every chirp stays within ±0.2 bin of its centre. Teal: the correction the tracker applied, +45 Hz by the end, matching the drift I measured by hand in §15.*

### 19.2 How much drift each version survives

I added extra drift, up and down, to my real frame:

![Drift tolerance: without tracking roughly −11 to +111 Hz; with tracking every tested case from −300 to +345 Hz](lora_aprs_pe_tolerance.svg)

| Total drift during the payload | Without tracking | With tracking (drift it reported) |
|---|---|---|
| −300 Hz | FAIL | OK (−299 Hz) |
| −100 Hz | FAIL | OK (−98 Hz) |
| −60, −45, −30 Hz | FAIL | OK |
| **−15 Hz** | **FAIL** | OK (−16 Hz) |
| +45 Hz (as recorded) | OK | OK (+44 Hz) |
| +95 Hz | OK | OK (+97 Hz) |
| +145, +195, +245 Hz | FAIL | OK |
| +345 Hz | FAIL | OK (+342 Hz) |

Every figure the tracker reported matches the drift I added to within a few hertz. That makes "frequency tracked" a genuine measurement of my tracker's oscillator, not just a debug number.

### 19.3 It didn't break anything else

| Test | Without tracking | With tracking |
|---|---|---|
| My real frame, noise added down to −20 dB in-band SNR | 6/6 | 6/6 |
| 12 copies of my real frame, −600 … +400 Hz tuning error (consistent clock) | 12/12 | 12/12 |
| Simulated frames, linear drift 40 Hz/s (~130 Hz per frame) | 3/5 | **5/5** |
| Simulated frames, linear drift 80 Hz/s (~260 Hz per frame) | 0/5 | **5/5** |
| Simulated sweep: 0 to −18 dB SNR, up to ±70 ppm crystal error | all decode | all decode |
| 30 simulated frames at −20 dB, six different noise patterns | 27/30 | 26/30 |

The last row is a one-frame difference at the very edge of SF12's sensitivity, within the noise of the test. Gentler gains didn't recover that frame either, so it isn't loop jitter.

### 19.4 And on air

With the updated flowgraph, my tracker decodes live.

---

## 20. The whole hunt in one picture, and what I took away from it

![The investigation step by step: symptom, suspects, recording, measurement, a dead end, consistent copies, the key observation, the test, the fix, back on air](lora_aprs_pe_investigation.svg)

**What I'm taking to my next project:**

1. **"Header fine, payload broken" points at something that grows with time.** The header is early and well protected; the payload is late and unprotected. Drift, sample loss and clock slip all produce this pattern.
2. **A recording beats speculation.** Three plausible suspects came down to one thanks to a single 32 MB file.
3. **Test signals must be physically consistent.** Frequency and clock come from the same crystal. When I moved one without the other, I got a convincing but false reproduction.
4. **Simulations only test what I put into them.** Every drift I simulated went upward, the direction my old receiver happened to tolerate, so the weakness stayed hidden until real data made me look closer.
5. **Look at the exact values, not just pass or fail.** "All 93 symbols on the lowest bin of their group" only became visible when I printed the decoded symbol values, and it explained everything.
6. **Margins can be one-sided.** A receiver that survives +110 Hz of drift can still fail at −15 Hz. I now always test both directions.

---

## 21. What my receiver still can't do

| Limitation | Why | What I can do about it |
|---|---|---|
| Tuning error beyond ±31 kHz (±72 ppm at 432 MHz) | The SFD reading wraps around | Correct the dongle with `rtl_ppm` if it has no TCXO |
| Drift beyond the tested ±300 Hz per frame | Untested: the loop may still follow, but I have no evidence | Watch the console's "frequency tracked" value to see how close I'm getting |
| Two frames overlapping on the channel | One synchroniser, one frame at a time | — |
| Implicit-header frames | The synchroniser always waits for a header | LoRa APRS always uses an explicit header |
| Samples lost on a busy laptop | Nothing can recover a gap mid-frame | Close heavy programs; the flowgraph itself runs well faster than real time |

**An improvement I haven't made yet.** Because LoRa always transmits on the *lowest* bin of each group, my decoder could decide "which group" by rounding to the nearest group start instead of rounding down. That would make the margin symmetric (about ±1.5 bins each way) even without tracking. With tracking already holding every chirp within ±0.2 bin of its centre on my real frame, the gain would be small. It would also need testing against more trackers than mine, because it relies on all of them placing symbols the same way.

---

## 22. Glossary

| Term | What I mean by it here |
|---|---|
| **APRS** | Automatic Packet Reporting System: position and message reports as short text lines |
| **TNC2 format** | The text form of an APRS packet: `SOURCE>DEST,PATH:information` |
| **LoRa APRS marker** | The three bytes `<` `0xFF` `0x01` at the start of every LoRa APRS packet |
| **Chirp / symbol** | One tone sweeping across the channel; its starting point carries the data |
| **Chip** | One of the 4096 steps of an SF12 chirp; 125,000 chips per second at 125 kHz |
| **SF (spreading factor)** | 12 here: 2¹² = 4096 chips per chirp; higher SF means slower and longer range |
| **Bin** | One frequency step of the 4096-point FFT: 30.5 Hz at SF12/125 kHz. The bin of the peak is the symbol value. |
| **Un-sweep / de-chirp** | Mixing a chirp with an opposite chirp so the sweep cancels and a steady tone remains |
| **FFT** | A frequency analyser that splits a slice into bins |
| **Argmax** | "Which bin is strongest?" |
| **Preamble** | The 8 identical chirps that announce a frame |
| **Sync word** | The network ID chirps (8 and 16 for 0x12) |
| **SFD** | Start-of-frame delimiter: 2¼ down-chirps that separate timing from tuning error and mark where the data begins |
| **Header** | The first 8 data chirps: length, coding rate, CRC flag; protected by CR 4/8 |
| **Payload** | The packet bytes plus CRC; at CR 4/5 it can detect but not correct errors |
| **CR (coding rate)** | 4/5 = four data bits per five sent; 4/8 = four per eight |
| **CRC-16** | The checksum the LoRa chip adds; the only integrity check in LoRa APRS |
| **Low data rate mode (LDRO)** | At SF11/12 with 125 kHz: only 10 of 12 bits per chirp are used, in groups of four bins |
| **Group of four** | The four bins that decode to the same 10 bits; LoRa transmits on the lowest of them |
| **CFO (tuning error)** | The frequency difference between transmitter and receiver |
| **Clock drift (SRO)** | The sample clock running slightly fast or slow, stretching each chirp |
| **ppm** | Parts per million; 1 ppm at 432 MHz = 432 Hz |
| **Drift (within a frame)** | The transmitter's frequency changing while it transmits, typically as its crystal warms |
| **Frequency tracking** | Re-measuring and correcting the frequency on every chirp |
| **Gate** | The test that only lets clearly decided chirps update the tracker |
| **TCXO** | Temperature-compensated crystal oscillator: about 1 ppm, and far less drift |
| **EPB** | Embedded Python Block: custom Python code inside a GNU Radio flowgraph |
| **Message port** | A GNU Radio connection that carries occasional messages (like "frame length 93") instead of a sample stream |
