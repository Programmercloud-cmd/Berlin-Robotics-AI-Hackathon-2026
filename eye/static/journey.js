const activityID = (name) => "activity:" + name;
const metricID = (name) => "metric:" + name;
const edgeID = (edge) => JSON.stringify([edge.source, edge.target, edge.kind, edge.provenance]);
const journey = ["session_started", "product_page_requested", "product_viewed", "cart_add_requested",
  "cart_added", "checkout_started", "payment_attempted", "payment_succeeded", "order_confirmed"].map(activityID);

function journeyLayout(graph, supporting = new Set()) {
  const ids = new Set(graph.nodes.map((node) => node.id));
  const positions = new Map();
  const connections = new Set();
  const precedes = graph.edges.filter((edge) => edge.kind === "precedes");
  const place = (id, x, y) => { if (ids.has(id)) positions.set(id, { x, y }); };
  const connect = (from, to) => {
    for (const edge of precedes) {
      if (edge.source === from && edge.target === to && positions.has(from) && positions.has(to)) {
        connections.add(edgeID(edge));
      }
    }
  };

  const stride = 142;
  const main = journey.filter((id) => ids.has(id));
  main.forEach((id, index) => place(id, index * stride, 0));
  main.slice(1).forEach((id, index) => connect(main[index], id));

  for (const [from, to, offset] of [
    ["product_page_requested", "product_page_load_failed", 0],
    ["cart_add_requested", "cart_add_failed", 0],
    ["payment_attempted", "payment_declined", -61],
    ["payment_attempted", "payment_timed_out", 61],
  ]) {
    const source = activityID(from);
    const target = activityID(to);
    if (positions.has(source) && precedes.some((edge) => edge.source === source && edge.target === target)) {
      place(target, positions.get(source).x + offset, 69);
      connect(source, target);
    }
  }

  const lanes = [
    { label: "Search", y: -225, steps: ["search_requested", "search_result_clicked"] },
    { label: "Recommendations", y: -105, steps: ["feed_impression", "feed_clicked"] },
  ];
  for (const lane of lanes) {
    lane.steps.forEach((name, index) => place(activityID(name), index * stride, lane.y));
    connect(activityID(lane.steps[0]), activityID(lane.steps[1]));
  }
  for (const [from, to] of [
    ["session_started", "search_requested"], ["session_started", "feed_impression"],
    ["search_requested", "feed_impression"], ["search_result_clicked", "product_page_requested"],
    ["feed_clicked", "product_page_requested"],
  ]) connect(activityID(from), activityID(to));

  const activityPositions = new Map(positions);
  const measured = graph.edges.filter((edge) => edge.kind === "measures" && activityPositions.has(edge.target));
  const preferred = {
    session_conversion_rate: "order_confirmed", checkout_completion_rate: "checkout_started",
    payment_success_rate: "payment_succeeded", payment_timeout_rate: "payment_timed_out",
    payment_timeouts: "payment_timed_out", other_failed_payments: "payment_declined",
    search_click_rate: "search_result_clicked", zero_result_searches: "search_requested",
  };
  const measures = new Set([
    "sessions", "product_view_sessions", "add_to_cart_sessions", "checkout_sessions", "checkout_completion_rate",
    "payment_attempts", "payment_success_rate", "payment_timeout_rate", "payment_timeouts", "successful_payments",
    "orders", "session_conversion_rate", "searches", "search_click_rate", "zero_result_searches",
    "feed_impressions", "feed_clicks", "unavailable_feed_share",
  ]);
  const metricRows = new Map();
  for (const node of graph.nodes) {
    if (!measures.has(node.properties.metric_id) && !supporting.has(node.id)) continue;
    const candidates = measured.filter((edge) => edge.source === node.id);
    const anchor = candidates.find((edge) => edge.target === activityID(preferred[node.properties.metric_id])) ||
      candidates.find((edge) => main.includes(edge.target)) || candidates[0];
    if (!anchor) continue;
    const { x, y } = activityPositions.get(anchor.target);
    const row = y < 0 ? y : x;
    const index = metricRows.get(row) || 0;
    metricRows.set(row, index + 1);
    if (y < 0) {
      place(node.id, (index + 2) * stride, y);
    } else {
      let metricY = 139 + index * 63;
      while ([...positions.values()].some((position) => Math.abs(position.x - x) < 118 &&
             Math.abs(position.y - metricY) < 60)) metricY += 63;
      place(node.id, x, metricY);
    }
    connections.add(edgeID(anchor));
  }

  // Delivery is observed through receipt timestamps and manifests. Keep their real graph sources.
  const deliveryMetrics = ["event_ingestion_lag_seconds", "data_completeness_ratio"].map(metricID).filter((id) => ids.has(id));
  const deliverySources = graph.edges.filter((edge) => edge.kind === "sourced_from" && deliveryMetrics.includes(edge.source));
  if (deliveryMetrics.length) {
    const row = Math.max(139, ...[...positions.values()].filter((point) => point.x >= 4 * stride)
      .map((point) => point.y)) + 120;
    deliveryMetrics.forEach((id, index) => place(id, (index + 5) * stride, row));
    const sources = [...new Set(deliverySources.map((edge) => edge.target))];
    sources.forEach((id, index) => place(id, (index + 5) * stride, row + 67));
    for (const edge of deliverySources) if (positions.has(edge.target)) connections.add(edgeID(edge));
    lanes.push({ label: "Receipt delivery", y: row, x: 4 * stride });
  }
  return {
    positions, connections,
    lanes: lanes.filter((lane) => !lane.steps || lane.steps.some((name) => positions.has(activityID(name)))),
  };
}

function incidentGraphFocus(graph, incident, rows) {
  const ids = new Set(graph.nodes.map((node) => node.id));
  const nodes = new Set();
  const affected = new Set();
  const supporting = new Set();
  const upstream = new Set();
  const connections = new Set();
  if (!incident) return { nodes, affected, supporting, upstream, connections };
  const report = incident.report || {};
  const completion = report.break;
  const root = metricID(incident.metric_id);
  const available = (values) => values.filter((id) => ids.has(id));
  available([...new Set([incident.metric_id, ...(completion?.metrics || [])])].map(metricID))
    .forEach((id) => supporting.add(id));
  const measureEdges = graph.edges.filter((edge) => edge.kind === "measures" && supporting.has(edge.source));
  const endpoints = completion?.activities?.map(activityID) || measureEdges.filter((edge) => edge.source === root)
    .map((edge) => edge.target);
  available(endpoints).forEach((id) => affected.add(id));
  [...supporting, ...affected].forEach((id) => nodes.add(id));
  function add(edge) {
    nodes.add(edge.source);
    nodes.add(edge.target);
    connections.add(edgeID(edge));
  }

  const branches = new Set((report.evidence_branches || []).filter((branch) => branch.supports?.length)
    .map((branch) => branch.evidence_id));
  const observations = new Set();
  function eventActivities(value) {
    if (!value || typeof value !== "object") return;
    const id = activityID(value.activity);
    if (ids.has(id)) observations.add(id);
    Object.values(value).forEach(eventActivities);
  }
  for (const row of rows) {
    if (row.kind === "branch evidence" && branches.has(row.id)) eventActivities(row.payload);
    if (row.kind === "observed events" && ids.has(row.node_id)) observations.add(row.node_id);
    if (row.kind !== "graph step") continue;
    const { source, target, kind, provenance } = row.payload;
    const observed = graph.edges.find((edge) => edge.source === source && edge.target === target &&
      edge.kind === kind && edge.provenance === provenance);
    if (observed) add(observed);
  }
  for (const edge of measureEdges) {
    if (!affected.size || affected.has(edge.target) || observations.has(edge.target)) add(edge);
  }
  for (const edge of graph.edges) {
    if ((edge.kind === "calculated_from" && supporting.has(edge.source) && supporting.has(edge.target)) ||
        (!affected.size && edge.kind === "sourced_from" && supporting.has(edge.source))) add(edge);
  }

  const successors = new Map();
  const ordering = ["session_started", "search_requested", "feed_impression", "feed_clicked", "search_result_clicked",
    "product_page_requested", "product_viewed", "product_page_load_failed", "cart_add_requested", "cart_added",
    "cart_add_failed", "checkout_started", "payment_attempted", "payment_declined", "payment_timed_out",
    "payment_succeeded", "order_confirmed"].map(activityID);
  const order = new Map(ordering.map((id, index) => [id, index]));
  const cohortTypes = ["session", "cart", "order", "payment", "search"];
  for (const edge of graph.edges) {
    if (edge.kind !== "precedes" || edge.source === edge.target) continue;
    if (order.has(edge.source) && order.has(edge.target) && order.get(edge.source) > order.get(edge.target)) continue;
    const populations = edge.properties?.object_types;
    if (populations && !cohortTypes.some((type) => populations[type])) continue;
    if (!successors.has(edge.source)) successors.set(edge.source, []);
    successors.get(edge.source).push(edge);
  }
  for (const choices of successors.values()) choices.sort((first, second) =>
    (second.properties?.object_types?.session || 0) - (first.properties?.object_types?.session || 0));

  function connectTo(start, targets) {
    const seen = new Set([start]);
    const queue = [[start, []]];
    for (const [node, path] of queue) {
      if (targets.has(node) && path.length) {
        path.forEach(add);
        return true;
      }
      for (const edge of successors.get(node) || []) {
        if (seen.has(edge.target)) continue;
        seen.add(edge.target);
        queue.push([edge.target, [...path, edge]]);
      }
    }
    return false;
  }

  if (completion && affected.size > 1) {
    const [entry, ...outcomes] = affected;
    connectTo(entry, new Set(outcomes));
  }
  for (const activity of observations) {
    if (!affected.size) {
      nodes.add(activity);
      upstream.add(activity);
      connectTo(activity, observations);
    } else if (!affected.has(activity) && connectTo(activity, affected)) {
      upstream.add(activity);
    }
  }
  return { nodes, affected, supporting, upstream, connections };
}
