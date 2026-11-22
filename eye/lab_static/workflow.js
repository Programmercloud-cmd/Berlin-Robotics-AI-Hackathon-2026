"use strict";

const labWorkflow = (() => {
  // This map represents simulator rules, independent of Eye's discovered graph.
  const steps = [
    ["search_requested", "Search request", 2, 1, 2],
    ["search_rollout", "Rollout", 2, 4, 2, "search", "cohort"],
    ["search_holdout", "Holdout", 3, 4, 2, null, "cohort"],
    ["search_results", "Search results", 2, 7, 2, "search"],
    ["feed_impression", "Recommendation exposure", 3, 7, 2, null, "optional", "Can appear while search is pending"],
    ["session_started", "Session start", 5, 1],
    ["product_page_requested", "Product page request", 5, 2],
    ["product_viewed", "Successful view", 5, 3, 1, "product_view"],
    ["cart_add_requested", "Add-to-cart request", 5, 4],
    ["cart_added", "Cart addition", 5, 5, 1, "cart_add"],
    ["stock_gate", "Stock available?", 5, 6, 1, null, "gate"],
    ["checkout_started", "Checkout", 5, 7],
    ["payment_attempted", "Payment attempt", 5, 8, 1, "payment"],
    ["payment_succeeded", "Authorized", 5, 9, 1, "payment", "authorization"],
    ["order_confirmed", "Order confirmed", 5, 10],
    ["product_page_load_failed", "Page load fails", 6, 2, 1, "product_view", "outcome"],
    ["cart_add_failed", "Cart add fails", 6, 4, 1, "cart_add", "outcome"],
    ["out_of_stock", "Checkout stops", 6, 6, 1, null, "outcome", "Out of stock"],
    ["payment_declined", "Declined", 6, 7, 1, "payment", "outcome"],
    ["payment_timed_out", "Gateway timeout", 6, 8, 1, "payment", "outcome"],
    ["order_paths", "Purchase paths", 6, 10, 1, null, "cohort"],
    ["success_receipt_in_transit", "Success receipt in transit", 8, 9, 1, "payment", "delivery"],
    ["success_receipt_received", "Receipt received", 8, 10, 1, "payment", "delivery"],
  ];
  const journey = steps.filter(([, , row]) => row === 5).map(([id]) => id);
  const links = journey.slice(1).map((id, i) => [journey[i], id, "right", "left", "main"]).concat([
    ["session_started", "search_requested", "top", "bottom", "branch"],
    ["search_requested", "search_results", "right", "left", "base"],
    ["search_requested", "feed_impression", "right", "left", "base"],
    ["search_requested", "search_rollout", "right", "left", "cohort"],
    ["search_requested", "search_holdout", "right", "left", "cohort"],
    ["search_rollout", "search_results", "right", "left", "cohort"],
    ["search_holdout", "search_results", "right", "left", "cohort"],
    ["search_holdout", "feed_impression", "right", "left", "cohort"],
    ["search_results", "product_page_requested", "right", "top", "journey-entry"],
    ["feed_impression", "product_page_requested", "bottom", "top", "journey-entry"],
    ["product_page_requested", "product_page_load_failed", "bottom", "top", "branch"],
    ["cart_add_requested", "cart_add_failed", "bottom", "top", "branch"],
    ["stock_gate", "out_of_stock", "bottom", "top", "branch"],
    ["payment_attempted", "payment_declined", "bottom", "top", "branch"],
    ["payment_attempted", "payment_timed_out", "bottom", "top", "branch"],
    ["order_confirmed", "order_paths", "bottom", "top", "cohort"],
    ["payment_succeeded", "success_receipt_in_transit", "bottom", "top", "delivery"],
    ["success_receipt_in_transit", "success_receipt_received", "right", "left", "delivery"],
  ]);
  const targets = {
    "reduce_completions/product_view": ["product_viewed", "product_page_load_failed"],
    "reduce_completions/cart_add": ["cart_added", "cart_add_failed"],
    "payment_timeout/payment": ["payment_timed_out"],
    "payment_success_delay/payment": ["success_receipt_in_transit"],
    "search_rollout/search": ["search_rollout"],
  };
  const numbers = new Intl.NumberFormat("en-GB");
  const rates = new Intl.NumberFormat("en-GB", { style: "percent", maximumFractionDigits: 1 });
  const rate = (numerator, denominator) => denominator ? rates.format(numerator / denominator) : "—";
  const count = (value) => numbers.format(value);

  function make(tag, text, className) {
    const item = document.createElement(tag);
    if (text != null) item.textContent = text;
    if (className) item.className = className;
    return item;
  }

  const board = document.getElementById("workflow-map");
  const canvas = make("canvas");
  canvas.setAttribute("aria-hidden", "true");
  board.append(canvas);
  for (const [row, label] of [[1, "Search & recommendations"], [4, "Shopping journey"], [7, "Event delivery"]]) {
    const heading = make("h3", label, "flow-lane");
    heading.style.gridRow = row;
    board.append(heading);
  }

  const nodes = new Map();
  for (const [id, label, row, column, width = 1, transition, kind = "main", detail] of steps) {
    const item = make("div", null, "flow-node flow-" + kind);
    item.dataset.step = id;
    if (transition) item.dataset.transition = transition;
    item.style.gridArea = row + " / " + column + " / auto / span " + width;
    item.append(make("strong", label));
    if (detail) item.append(make("small", detail));
    const reading = make("small", null, "flow-reading");
    reading.hidden = true;
    item.append(reading);
    board.append(item);
    nodes.set(id, { item, reading, kind });
  }

  let selected = new Set();
  let experiment = false;
  let phase = "base";
  let frame;

  function repaint() {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(draw);
  }

  function anchor(rect, side, origin) {
    const midX = rect.left + rect.width / 2 - origin.left;
    const midY = rect.top + rect.height / 2 - origin.top;
    switch (side) {
      case "left": return [rect.left - origin.left, midY, -1, 0];
      case "right": return [rect.right - origin.left, midY, 1, 0];
      case "top": return [midX, rect.top - origin.top, 0, -1];
      default: return [midX, rect.bottom - origin.top, 0, 1];
    }
  }

  function draw() {
    const origin = board.getBoundingClientRect();
    const density = devicePixelRatio || 1;
    canvas.width = Math.round(origin.width * density);
    canvas.height = Math.round(origin.height * density);
    const context = canvas.getContext("2d");
    context.scale(density, density);
    const palette = getComputedStyle(board);
    const muted = palette.getPropertyValue("--muted").trim();
    const accent = palette.getPropertyValue("--accent").trim();
    for (const [from, to, fromSide, toSide, kind] of links) {
      if ((kind === "base" && experiment) || (kind === "cohort" && !experiment)) continue;
      const start = anchor(nodes.get(from).item.getBoundingClientRect(), fromSide, origin);
      const end = anchor(nodes.get(to).item.getBoundingClientRect(), toSide, origin);
      const [x, y, dx, dy] = start;
      const [nx, ny, tx, ty] = end;
      const approach = (vx, vy) => Math.min(74, Math.max(7, Math.abs((nx - x) * vx + (ny - y) * vy) * .5));
      const highlighted = selected.has(to) || (experiment && from === "search_rollout");
      context.strokeStyle = highlighted ? accent : muted;
      context.fillStyle = context.strokeStyle;
      context.globalAlpha = highlighted || kind === "main" ? 1 : .62;
      context.lineWidth = highlighted ? 2 : kind === "main" ? 1.45 : 1.1;
      context.setLineDash(highlighted && phase === "preview" ? [4, 3] : []);
      context.beginPath();
      context.moveTo(x, y);
      if (kind === "journey-entry") {
        const routeY = ny - (fromSide === "right" ? 23 : 12);
        const routeX = fromSide === "right" ? x + 9 : x;
        context.lineTo(routeX, y);
        context.lineTo(routeX, routeY);
        context.lineTo(nx, routeY);
        context.lineTo(nx, ny);
      } else {
        const departure = approach(dx, dy);
        const arrival = approach(tx, ty);
        context.bezierCurveTo(x + dx * departure, y + dy * departure, nx + tx * arrival, ny + ty * arrival, nx, ny);
      }
      context.stroke();
      context.setLineDash([]);
      context.beginPath();
      context.moveTo(nx, ny);
      context.lineTo(nx + tx * 6 - ty * 3, ny + ty * 6 + tx * 3);
      context.lineTo(nx + tx * 6 + ty * 3, ny + ty * 6 - tx * 3);
      context.fill();
    }
  }

  function annotations(readings) {
    const values = {};
    if (!readings) return values;
    const counts = readings.counts;
    if (readings.transition === "payment") {
      Object.assign(values, {
        payment_attempted: count(counts.attempted) + " attempts",
        payment_succeeded: rate(counts.authorized, counts.attempted) + " authorized",
        payment_declined: count(counts.declined) + " declines",
        payment_timed_out: count(counts.timed_out) + " timeouts",
        order_confirmed: count(counts.confirmed) + " orders",
        success_receipt_in_transit: count(counts.in_transit_at_cutoff) + " at cutoff",
        success_receipt_received: count(counts.received_since_cutoff) + " arrived later",
      });
    } else if (readings.transition === "search") {
      for (const [arm, cohort] of Object.entries(readings.cohorts)) {
        values["search_" + arm] = cohort.releases.join(", ") + " · " + count(cohort.sessions) +
          " sessions · " + count(cohort.median_response_ms) + " ms median";
      }
      values.order_paths = "Direct or assisted";
    } else {
      const product = readings.transition === "product_view";
      values[product ? "product_page_requested" : "cart_add_requested"] = count(counts.requested) + " sessions";
      values[product ? "product_viewed" : "cart_added"] = rate(counts.completed, counts.requested) + " completed";
      values[product ? "product_page_load_failed" : "cart_add_failed"] = count(counts.failed) + " failures";
    }
    return values;
  }

  function update(plan, nextPhase, readings) {
    const effect = plan?.effect + "/" + plan?.transition;
    selected = new Set(targets[effect] || []);
    phase = nextPhase;
    experiment = plan?.effect === "search_rollout";
    board.dataset.phase = phase;
    const values = annotations(readings);
    for (const [id, { item, reading, kind }] of nodes) {
      item.hidden = kind === "cohort" && !experiment;
      item.toggleAttribute("data-affected", selected.has(id));
      reading.textContent = values[id] || (experiment && id === "order_paths" ? "Direct or assisted" : "");
      reading.hidden = !reading.textContent;
    }
    repaint();
  }

  new ResizeObserver(repaint).observe(board);
  new MutationObserver(repaint).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  update(null, "base", null);
  return { update, rate, count };
})();
