// slides.typ — versioned 16:9 deck template for the content-to-slides engine.
// v1 (docs/content-to-slides.md §5.1). Style-only: #set/#let definitions and named
// slide functions; the emitter (typst_deck.py) appends the deterministic body that
// calls them. Golden-snapshot tests pin compiled output — bump
// SLIDES_TEMPLATE_VERSION whenever this file changes.
//
// v2 (plan §M1.7 — Visual Compiler): adds the brief-native templates
// thesisSlide / figureSlide / funnelSlide / loopSlide / tableSlide. All v1
// functions are untouched, so legacy DeckSpec emits keep compiling identically.
//
// Contract with the emitter:
//   * every text field arrives ALREADY WRAPPED as an array of lines — the template
//     never re-wraps, so measurement and rendering can never disagree;
//   * every *_mm / *_pt geometry value is a unitless number; the template multiplies;
//   * the layout engine guarantees fit upstream — no trimming here or there.
// Page constants below MUST mirror deck/layout.py.

#let SLIDES_TEMPLATE_VERSION = 2

// ── geometry (mirror of layout.py) ───────────────────────────────────────────
#let CONTENT-W = 306.67          // PAGE-W 338.67 - 2 * MARGIN-X 16
#let BODY-H = 166.5              // PAGE-H 190.5 - 2 * MARGIN-Y 12
#let HEADER-H = 26               // title + kicker band
#let SLOT-H = 140.5              // BODY-H - HEADER-H — the slot every visual fills

// ── palette & fonts ──────────────────────────────────────────────────────────
#let deck-primary = rgb("#1F3A5F")
#let deck-accent  = rgb("#C4531B")
#let deck-ink     = rgb("#22303C")
#let deck-muted   = rgb("#5B6B7A")
#let deck-faint   = rgb("#8A97A3")
#let deck-line    = rgb("#B9C4CF")
#let deck-card    = rgb("#F4F6F8")
#let deck-band    = rgb("#EEF2F6")
#let series-colors = (deck-primary, deck-accent)

#set page(
  width: 338.67mm,
  height: 190.5mm,
  margin: (left: 16mm, right: 16mm, top: 12mm, bottom: 12mm),
  fill: white,
)
#set text(
  font: ("Libertinus Serif", "Noto Serif SC", "DejaVu Sans Mono"),
  size: 16pt,
  fill: deck-ink,
  lang: "zh",
  region: "cn",
)

// ── shared primitives ────────────────────────────────────────────────────────

// Render pre-wrapped lines at a fixed size tier. Never modifies the text.
// NOTE: the body is built in CODE mode — inside markup content `[...]` a `*` parses
// as emphasis, which is why `set text(size: size * 1pt, ...)` must not live in `[...]`.
#let textLines(arr, size, fill: deck-ink, weight: "regular", at: left + horizon) = {
  if arr.len() == 0 {
    []
  } else {
    align(at, {
      set text(size: size * 1pt, fill: fill, weight: weight)
      set par(leading: 0.30em, justify: false, spacing: 0pt)
      arr.join(linebreak())
    })
  }
}

#let citeLine(arr) = {
  if arr.len() > 0 {
    set text(size: 8.5pt, fill: deck-faint)
    arr.join([ · ])
  } else {
    []
  }
}

// Common slide chrome: fixed header band, body slot, bottom-left citations.
#let slide(d, body) = block(width: 100%, height: BODY-H * 1mm)[
  #box(width: 100%, height: HEADER-H * 1mm)[
    #textLines(d.title_lines, d.title_pt, fill: deck-primary, weight: "bold",
               at: left + bottom)
    #textLines(d.kicker_lines, d.kicker_pt, fill: deck-muted, at: left + top)
  ]
  #block(width: 100%, height: SLOT-H * 1mm)[#body]
  #place(bottom + left)[#citeLine(d.citations)]
]

#let connector(wmm, hmm) = box(width: wmm * 1mm, height: hmm * 1mm)[
  #place(left + horizon)[
    #line(length: wmm * 1mm - 7pt, stroke: 0.9pt + deck-primary)
  ]
  #place(right + horizon, dx: -0.5pt)[
    #polygon((0pt, -3.5pt), (6pt, 0pt), (0pt, 3.5pt), fill: deck-primary)
  ]
]

// ── cover ────────────────────────────────────────────────────────────────────
#let deckCover(d) = align(center + horizon)[
  #textLines(d.title_lines, 34, fill: deck-primary, weight: "bold",
             at: center + horizon)
  #v(7mm)
  #textLines(d.subtitle_lines, 15, fill: deck-muted, at: center + horizon)
  #v(12mm)
  #set text(size: 10.5pt, fill: deck-faint)
  #d.sources_label: #d.sources.join(", ")
]

// ── TEXT_HERO ────────────────────────────────────────────────────────────────
#let heroSlide(d) = slide(d)[
  #align(center + horizon)[
    #textLines(d.message_lines, d.message_pt,
               fill: if d.emphasis == "primary" { deck-primary } else { deck-ink },
               weight: "bold", at: center + horizon)
  ]
]

// ── CARDS ────────────────────────────────────────────────────────────────────
#let cardsSlide(d) = {
  let b = d.body
  let cards = b.cards.map(c => box(
    width: 100%,
    height: b.slot_h_mm * 1mm,
    radius: 4pt,
    fill: deck-card,
    stroke: 0.7pt + deck-line,
    inset: 6pt,
  )[
    #textLines(c.label_lines, c.label_pt, fill: deck-primary, weight: "bold",
               at: top + left)
    #v(3pt)
    #textLines(c.detail_lines, c.detail_pt, fill: deck-ink, at: top + left)
  ])
  slide(d)[
    #align(center + horizon)[
      #grid(columns: b.cols, rows: b.rows, gutter: b.gap_mm * 1mm, ..cards)
    ]
  ]
}

// ── FLOWCHART ────────────────────────────────────────────────────────────────
#let flowSlide(d) = {
  let b = d.body
  let items = ()
  for i in range(b.steps.len()) {
    let st = b.steps.at(i)
    items.push(box(
      width: b.node_w_mm * 1mm,
      height: b.node_h_mm * 1mm,
      radius: 4pt,
      fill: deck-card,
      stroke: 0.8pt + deck-primary,
      inset: 4pt,
    )[
      #textLines(st.label_lines, st.label_pt, fill: deck-primary, weight: "bold")
      #v(2pt)
      #textLines(st.detail_lines, st.detail_pt, fill: deck-muted)
    ])
    if i < b.n - 1 {
      items.push(connector(b.gap_mm, b.node_h_mm))
    }
  }
  slide(d)[
    #align(center + horizon)[
      #stack(dir: ltr, spacing: 0mm, ..items)
    ]
  ]
}

// ── TIMELINE ─────────────────────────────────────────────────────────────────
#let timelineSlide(d) = {
  let b = d.body
  let slot = CONTENT-W / b.n
  let axisY = SLOT-H * 0.42
  let marks = ()
  for i in range(b.steps.len()) {
    let st = b.steps.at(i)
    let cx = slot * (i + 0.5) * 1mm
    let half = (slot * 1mm - 3mm) / 2
    marks.push(place(top + left, dx: cx - half, dy: (axisY - 10) * 1mm)[
      #box(width: slot * 1mm - 3mm)[
        #textLines(st.when_lines, st.when_pt, fill: deck-accent,
                   weight: "bold", at: center + horizon)
      ]
    ])
    marks.push(place(top + left, dx: cx - 2.8pt, dy: axisY * 1mm - 2.8pt)[
      #circle(radius: 2.8pt, fill: deck-accent, stroke: none)
    ])
    marks.push(place(top + left, dx: cx - half, dy: (axisY + 3) * 1mm)[
      #box(width: slot * 1mm - 3mm)[
        #textLines(st.label_lines, st.label_pt, fill: deck-primary,
                   weight: "bold", at: center + top)
        #textLines(st.detail_lines, st.detail_pt, fill: deck-ink,
                   at: center + top)
      ]
    ])
  }
  slide(d)[
    #align(horizon)[
      #box(width: CONTENT-W * 1mm, height: SLOT-H * 0.85 * 1mm)[
        #place(top + left, dy: axisY * 1mm)[
          #line(length: CONTENT-W * 1mm, stroke: 1pt + deck-primary)
        ]
        #{ marks.join() }
      ]
    ]
  ]
}

// ── COMPARISON ───────────────────────────────────────────────────────────────
#let compareSlide(d) = {
  let b = d.body
  let hdr = b.cols.map(c =>
    textLines(c.header_lines, c.header_pt, fill: white, weight: "bold",
              at: left + horizon))
  let nrows = if b.cols.len() > 0 { b.cols.at(0).cells.len() } else { 0 }
  let cells = ()
  for r in range(nrows) {
    for c in b.cols {
      cells.push(textLines(c.cells.at(r).lines, c.cells.at(r).pt,
                           at: left + horizon))
    }
  }
  slide(d)[
    #align(center + horizon)[
      #table(
        columns: b.cols.map(c => 1fr),
        inset: 5pt,
        stroke: 0.5pt + deck-line,
        fill: (x, y) => {
          if y == 0 { deck-primary }
          else if calc.even(y - 1) { rgb("#FAFBFC") }
          else { white }
        },
        table.header(..hdr),
        ..cells,
      )
    ]
  ]
}

// ── ARCHITECTURE ─────────────────────────────────────────────────────────────
#let archSlide(d) = {
  let b = d.body
  let bands = b.bands.map(g => box(
    width: 100%,
    height: b.band_h_mm * 1mm * 0.9,
    radius: 3pt,
    fill: deck-band,
    stroke: 0.6pt + deck-line,
    inset: 3pt,
  )[
    #textLines(g.group_lines, g.group_pt, fill: deck-muted, weight: "bold",
               at: left + top)
    #v(2pt)
    #stack(dir: ltr, spacing: 4mm, ..g.nodes.map(n => box(
      width: g.node_w_mm * 1mm,
      radius: 3pt,
      fill: white,
      stroke: 0.8pt + deck-primary,
      inset: 4pt,
    )[
      #textLines(n.lines, n.pt, fill: deck-primary, weight: "bold",
                 at: center + horizon)
    ]))
  ])
  slide(d)[
    #align(horizon)[
      #stack(dir: ttb, spacing: b.gap_mm * 1mm, ..bands)
    ]
  ]
}

// ── CHART (bars / polylines from quantized data only) ────────────────────────
#let chartSlide(d) = {
  let b = d.body
  let pw = b.plot_w_mm * 1mm
  let ph = b.plot_h_mm * 1mm
  let npts = if b.series.len() > 0 { b.series.at(0).points.len() } else { 1 }
  let slotw = b.plot_w_mm / npts
  let nser = b.series.len()
  // all plot marks are built in CODE mode (markup `[...]` would eat `*` and `let`)
  let marks = ()
  for i in range(b.grid) {
    marks.push(place(top + left, dy: (b.plot_h_mm * i / b.grid) * 1mm)[
      #line(length: pw, stroke: (paint: rgb("#E1E7EC"), thickness: 0.6pt,
                                 dash: "dashed"))
    ])
  }
  marks.push(place(top + left, dy: ph)[
    #line(length: pw, stroke: 0.8pt + deck-line)
  ])
  if b.kind == "bar" {
    for si in range(nser) {
      let s = b.series.at(si)
      for xi in range(s.points.len()) {
        let p = s.points.at(xi)
        let bw = slotw * 0.62 / nser * 1mm
        let bx = (slotw * xi + slotw * 0.19) * 1mm + bw * si
        let bh = b.plot_h_mm * 1mm - p.y_mm * 1mm
        if bh > 0pt {
          marks.push(place(bottom + left, dx: bx)[
            #box(width: bw, height: bh, fill: series-colors.at(si))
          ])
        }
      }
    }
  } else {
    for si in range(nser) {
      let s = b.series.at(si)
      let col = series-colors.at(si)
      for p in s.points {
        marks.push(place(top + left, dx: p.x_mm * 1mm - 1.8pt,
                         dy: p.y_mm * 1mm - 1.8pt)[
          #circle(radius: 1.8pt, fill: col, stroke: none)
        ])
      }
      let pts = s.points.map(p => (p.x_mm, p.y_mm))
      for i in range(1, pts.len()) {
        let a = pts.at(i - 1)
        let c = pts.at(i)
        // unitless mm math — lengths cannot be squared in Typst
        let dxn = c.at(0) - a.at(0)
        let dyn = c.at(1) - a.at(1)
        marks.push(place(top + left, dx: a.at(0) * 1mm, dy: a.at(1) * 1mm)[
          #line(length: calc.sqrt(dxn * dxn + dyn * dyn) * 1mm,
                angle: calc.atan2(dyn, dxn), stroke: 1.4pt + col)
        ])
      }
    }
  }
  if b.series.len() > 0 {
    for xi in range(npts) {
      let p = b.series.at(0).points.at(xi)
      marks.push(place(top + left, dx: slotw * xi * 1mm, dy: ph + 1.5mm)[
        #box(width: slotw * 1mm)[
          #textLines(p.label_lines, 8, fill: deck-muted, at: center + top)
        ]
      ])
    }
  }
  let legend = ()
  if nser > 1 {
    for si in range(nser) {
      let s = b.series.at(si)
      legend.push(align(left, {
        set text(size: 9pt, fill: deck-muted)
        [#box(width: 7pt, height: 7pt, radius: 2pt,
              fill: series-colors.at(si))#s.name]
      }))
    }
  }
  slide(d)[
    #align(center + horizon)[
      #box(width: pw, height: ph + 12mm)[#{ marks.join() }]
      #if legend.len() > 0 [
        #v(4pt)
        #align(center)[#{ legend.join() }]
      ]
    ]
  ]
}

// ── v2: brief-native templates (Visual Compiler, plan §M1.7) ────────────────

// CENTERED_THESIS — one big statement + optional chip row of card labels.
#let thesisSlide(d) = {
  let chip(s) = box(radius: 12pt, fill: deck-band, inset: (x: 9pt, y: 3pt))[
    #set text(size: 10pt, fill: deck-muted)
    #s
  ]
  slide(d)[
    #align(center + horizon)[
      #textLines(d.message_lines, d.message_pt, fill: deck-primary,
                 weight: "bold", at: center + horizon)
      #if d.chips.len() > 0 [
        #v(7mm)
        #align(center)[#stack(dir: ltr, spacing: 4mm, ..d.chips.map(s => chip(s)))]
      ]
    ]
  ]
}

// DATA_DASHBOARD (source slice) — contained figure + caption + optional notes.
// b.name is a workdir-relative file copied in by render.render_brief_pdf.
#let figureSlide(d) = {
  let b = d.body
  let notes = b.notes.cards.map(c => box(
    width: 100%,
    radius: 4pt,
    fill: deck-card,
    stroke: 0.7pt + deck-line,
    inset: 6pt,
  )[
    #textLines(c.label_lines, c.label_pt, fill: deck-primary, weight: "bold",
               at: top + left)
    #v(3pt)
    #textLines(c.detail_lines, c.detail_pt, fill: deck-ink, at: top + left)
  ])
  let figure = [
    #align(center)[
      // w/h are already contain-fit computed upstream — exact box, no stretch
      #image(b.name, width: b.w_mm * 1mm, height: b.h_mm * 1mm)
    ]
    #if b.caption_lines.len() > 0 [
      #v(2pt)
      #align(center)[
        #textLines(b.caption_lines, b.caption_pt, fill: deck-faint,
                   at: center + top)
      ]
    ]
  ]
  slide(d)[
    #if notes.len() > 0 {
      grid(
        columns: (b.img_col_fr * 1fr, 1fr),
        gutter: b.gap_mm * 1mm,
        align(horizon)[#figure],
        grid(columns: 1, rows: notes.len(), gutter: 2mm, ..notes),
      )
    } else {
      align(center + horizon)[#figure]
    }
  ]
}

// HORIZONTAL_FLOW (funnel variant) — decreasing-width levels, python tints.
#let funnelSlide(d) = {
  let b = d.body
  let levels = b.levels.map(l => align(center)[
    #box(
      width: l.w_mm * 1mm,
      height: b.level_h_mm * 1mm,
      radius: 3pt,
      fill: rgb(l.fill),
      inset: 4pt,
    )[
      #textLines(l.label_lines, l.label_pt, fill: rgb(l.text), weight: "bold",
                 at: center + horizon)
      #textLines(l.detail_lines, l.detail_pt, fill: rgb(l.text),
                 at: center + horizon)
    ]
  ])
  slide(d)[
    #align(center + horizon)[
      #stack(dir: ttb, spacing: b.gap_mm * 1mm, ..levels)
    ]
  ]
}

// HORIZONTAL_FLOW (loop variant) — flow row + dashed feedback return beneath.
#let loopSlide(d) = {
  let b = d.body
  let items = ()
  for i in range(b.steps.len()) {
    let st = b.steps.at(i)
    items.push(box(
      width: b.node_w_mm * 1mm,
      height: b.node_h_mm * 1mm,
      radius: 4pt,
      fill: deck-card,
      stroke: 0.8pt + deck-primary,
      inset: 4pt,
    )[
      #textLines(st.label_lines, st.label_pt, fill: deck-primary, weight: "bold")
      #v(2pt)
      #textLines(st.detail_lines, st.detail_pt, fill: deck-muted)
    ])
    if i < b.n - 1 {
      items.push(connector(b.gap_mm, b.node_h_mm))
    }
  }
  let lineLen = CONTENT-W - b.gap_mm * 2
  slide(d)[
    #align(center + horizon)[
      #box(width: CONTENT-W * 1mm, height: (b.node_h_mm + 14) * 1mm)[
        #place(top + center)[#stack(dir: ltr, spacing: 0mm, ..items)]
        #place(top + left, dx: b.gap_mm * 1mm, dy: (b.node_h_mm + 8) * 1mm)[
          #line(length: lineLen * 1mm,
                stroke: (dash: "dashed", thickness: 0.9pt, paint: deck-accent))
        ]
        #place(top + left, dx: (b.gap_mm - 0.5) * 1mm,
               dy: (b.node_h_mm + 4.5) * 1mm)[
          #polygon((6pt, 0pt), (0pt, 3.5pt), (6pt, 7pt), fill: deck-accent)
        ]
        #place(top + right, dx: -b.gap_mm * 1mm, dy: (b.node_h_mm + 9.5) * 1mm)[
          #set text(size: 8pt, fill: deck-accent)
          #b.loop_label
        ]
      ]
    ]
  ]
}

// NATIVE_TABLE — two-column label | detail rows from content cards.
#let tableSlide(d) = {
  let b = d.body
  let cells = ()
  for r in b.rows {
    cells.push(textLines(r.label_lines, r.label_pt, fill: deck-primary,
                         weight: "bold"))
    cells.push(textLines(r.detail_lines, r.detail_pt, fill: deck-ink))
  }
  slide(d)[
    #align(center + horizon)[
      #table(
        columns: (b.left_fr * 1fr, 1fr),
        inset: 6pt,
        stroke: 0.5pt + deck-line,
        fill: (x, y) => {
          if calc.even(y) { white } else { rgb("#FAFBFC") }
        },
        ..cells,
      )
    ]
  ]
}

// ── emitted body (deterministic — do not edit by hand) ──────
#deckCover((title_lines: ("测试 Deck",), subtitle_lines: (), sources_label: "Sources", sources: ("doc.md",)))
#pagebreak()
#heroSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: (), kicker_pt: 16, message_lines: ("少做 re-verify,多留证据复用",), message_pt: 34, emphasis: "primary", citations: ("[src1:1-3]",)))
#pagebreak()
#cardsSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: ("这一页只讲一个结论",), kicker_pt: 16, body: (cols: 4, rows: 1, gap_mm: 8.0, slot_w_mm: 70.67, slot_h_mm: 140.5, cards: ((label_lines: ("概念1",), label_pt: 16, detail_lines: ("说明 1 explains itself",), detail_pt: 12), (label_lines: ("概念2",), label_pt: 16, detail_lines: ("说明 2 explains itself",), detail_pt: 12), (label_lines: ("概念3",), label_pt: 16, detail_lines: ("说明 3 explains itself",), detail_pt: 12), (label_lines: ("概念4",), label_pt: 16, detail_lines: ("说明 4 explains itself",), detail_pt: 12)), direction: "vertical"), citations: ("[src1:1-3]",)))
#pagebreak()
#flowSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: ("这一页只讲一个结论",), kicker_pt: 16, body: (n: 5, gap_mm: 10.0, node_w_mm: 53.33, node_h_mm: 30.0, steps: ((label_lines: ("步骤1",), label_pt: 16, detail_lines: ("step 1 does work",), detail_pt: 12, when_lines: (), when_pt: 12), (label_lines: ("步骤2",), label_pt: 16, detail_lines: ("step 2 does work",), detail_pt: 12, when_lines: (), when_pt: 12), (label_lines: ("步骤3",), label_pt: 16, detail_lines: ("step 3 does work",), detail_pt: 12, when_lines: (), when_pt: 12), (label_lines: ("步骤4",), label_pt: 16, detail_lines: ("step 4 does work",), detail_pt: 12, when_lines: (), when_pt: 12), (label_lines: ("步骤5",), label_pt: 16, detail_lines: ("step 5 does work",), detail_pt: 12, when_lines: (), when_pt: 12))), citations: ("[src1:1-3]",)))
#pagebreak()
#timelineSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: ("这一页只讲一个结论",), kicker_pt: 16, body: (n: 4, gap_mm: 6.0, node_w_mm: 72.17, node_h_mm: 0.0, steps: ((label_lines: ("步骤1",), label_pt: 16, detail_lines: ("step 1 does work",), detail_pt: 12, when_lines: ("2021-01",), when_pt: 12), (label_lines: ("步骤2",), label_pt: 16, detail_lines: ("step 2 does work",), detail_pt: 12, when_lines: ("2022-01",), when_pt: 12), (label_lines: ("步骤3",), label_pt: 16, detail_lines: ("step 3 does work",), detail_pt: 12, when_lines: ("2023-01",), when_pt: 12), (label_lines: ("步骤4",), label_pt: 16, detail_lines: ("step 4 does work",), detail_pt: 12, when_lines: ("2024-01",), when_pt: 12))), citations: ("[src1:1-3]",)))
#pagebreak()
#compareSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: ("这一页只讲一个结论",), kicker_pt: 16, body: (cols: ((header_lines: ("宽松",), header_pt: 16, cells: ((lines: ("快",), pt: 12), (lines: ("省",), pt: 12), (lines: ("默认",), pt: 12))), (header_lines: ("严格",), header_pt: 16, cells: ((lines: ("稳",), pt: 12), (lines: ("全",), pt: 12), (lines: ("审计",), pt: 12)))), gap_mm: 6.0, col_w_mm: 150.34), citations: ("[src1:1-3]",)))
#pagebreak()
#archSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: ("这一页只讲一个结论",), kicker_pt: 16, body: (bands: ((group_lines: ("接入层",), group_pt: 12, nodes: ((lines: ("API",), pt: 16, detail: ""),), node_w_mm: 145.34), (group_lines: ("执行层",), group_pt: 12, nodes: ((lines: ("Worker",), pt: 16, detail: ""),), node_w_mm: 145.34), (group_lines: ("存储层",), group_pt: 12, nodes: ((lines: ("Store",), pt: 16, detail: ""), (lines: ("Drive",), pt: 16, detail: "")), node_w_mm: 94.22)), band_h_mm: 41.5, gap_mm: 8.0, direction: "horizontal"), citations: ("[src1:1-3]",)))
#pagebreak()
#chartSlide((title_lines: ("单页标题",), title_pt: 26, kicker_lines: ("这一页只讲一个结论",), kicker_pt: 16, body: (kind: "line", plot_w_mm: 286.67, plot_h_mm: 116.5, v_min: 0.0, v_max: 72.0, series: ((name: "指标", points: ((x_mm: 71.67, y_mm: 0.0, value: 72.0, label_lines: ("2024",)), (x_mm: 215.0, y_mm: 115.59, value: 0.565, label_lines: ("2025",)))),), grid: 4), citations: ("[src1:1-3]",)))
