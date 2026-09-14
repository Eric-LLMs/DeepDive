// slides.typ — versioned 16:9 deck template for the content-to-slides engine.
// v1 (docs/content-to-slides.md §5.1). Style-only: #set/#let definitions and named
// slide functions; the emitter (typst_deck.py) appends the deterministic body that
// calls them. Golden-snapshot tests pin compiled output — bump
// SLIDES_TEMPLATE_VERSION whenever this file changes.
//
// Contract with the emitter:
//   * every text field arrives ALREADY WRAPPED as an array of lines — the template
//     never re-wraps, so measurement and rendering can never disagree;
//   * every *_mm / *_pt geometry value is a unitless number; the template multiplies;
//   * the layout engine guarantees fit upstream — no trimming here or there.
// Page constants below MUST mirror deck/layout.py.

#let SLIDES_TEMPLATE_VERSION = 1

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
