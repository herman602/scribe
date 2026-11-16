"use strict";

const palette = ["#176b4d", "#c2842a", "#54708b", "#a54b42", "#7b6ca8", "#4d8781"];

function majorValues(values, digits) {
  const scale = 10 ** digits;
  return values.map((value) => value / scale);
}

function moneyFormatter(currency, displayDigits) {
  return new Intl.NumberFormat("zh-TW", {
    style: "currency",
    currency,
    minimumFractionDigits: displayDigits,
    maximumFractionDigits: displayDigits,
  });
}

function moneyOptions(currency, displayDigits) {
  const formatter = moneyFormatter(currency, displayDigits);
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (context) => formatter.format(Number(context.raw)),
        },
      },
    },
    scales: {
      y: {
        beginAtZero: true,
        ticks: { callback: (value) => formatter.format(value) },
      },
    },
  };
}

function lineChart(id, labels, values, currency, digits, displayDigits) {
  const element = document.getElementById(id);
  if (!element) return;
  new Chart(element, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: majorValues(values, digits),
        borderColor: palette[0],
        backgroundColor: "rgba(23, 107, 77, 0.12)",
        borderWidth: 2,
        fill: true,
        pointRadius: labels.length > 100 ? 0 : 2,
        tension: 0.2,
      }],
    },
    options: moneyOptions(currency, displayDigits),
  });
}

function rankingChart(id, labels, values, currency, digits, displayDigits) {
  const element = document.getElementById(id);
  if (!element) return;
  const options = moneyOptions(currency, displayDigits);
  options.indexAxis = "y";
  options.scales = {
    x: {
      beginAtZero: true,
      ticks: { callback: (value) => moneyFormatter(currency, displayDigits).format(value) },
    },
  };
  new Chart(element, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: majorValues(values, digits),
        backgroundColor: labels.map((_, index) => palette[index % palette.length]),
        borderRadius: 5,
      }],
    },
    options,
  });
}

function typeChart(types) {
  const element = document.getElementById("type-distribution");
  if (!element || !types.labels.length) return;
  new Chart(element, {
    type: "doughnut",
    data: {
      labels: types.labels,
      datasets: [{
        data: types.values,
        backgroundColor: types.labels.map((_, index) => palette[index % palette.length]),
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" } },
    },
  });
}

function renderStatistics() {
  const source = document.getElementById("statistics-data");
  if (!source || typeof Chart === "undefined") return;
  const data = JSON.parse(source.textContent);
  data.currencies.forEach((item) => {
    lineChart(`daily-${item.currency}`, item.daily.labels, item.daily.values, item.currency, item.minor_digits, item.display_digits);
    lineChart(`monthly-${item.currency}`, item.monthly.labels, item.monthly.values, item.currency, item.minor_digits, item.display_digits);
    rankingChart(`merchants-${item.currency}`, item.merchants.labels, item.merchants.values, item.currency, item.minor_digits, item.display_digits);
    rankingChart(`cards-${item.currency}`, item.cards.labels, item.cards.values, item.currency, item.minor_digits, item.display_digits);
  });
  typeChart(data.types);
}

document.addEventListener("DOMContentLoaded", renderStatistics);
