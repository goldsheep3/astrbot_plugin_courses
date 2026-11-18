const bridge = window.AstrBotPluginPage;
const elements = {
  user: document.getElementById("user-select"),
  date: document.getElementById("date-input"),
  title: document.getElementById("term-title"),
  week: document.getElementById("week-label"),
  number: document.getElementById("week-number"),
  grid: document.getElementById("week-grid"),
  status: document.getElementById("status"),
};

let selectedDate = formatDate(new Date());
let requestNumber = 0;

function formatDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftDate(isoDate, days) {
  const value = new Date(`${isoDate}T12:00:00`);
  value.setDate(value.getDate() + days);
  return formatDate(value);
}

function readableDate(isoDate) {
  const [, month, day] = isoDate.split("-");
  return `${Number(month)}月${Number(day)}日`;
}

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle("error", isError);
  elements.status.hidden = !message;
}

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function eventCard(item) {
  const card = document.createElement("article");
  const exam = item.kind === "exam";
  card.className = `event ${exam ? "exam" : item.source === "override" ? "changed" : "course"}`;
  const time = [item.start, item.end].filter(Boolean).join("–") || "时间待定";
  card.append(textNode("p", time, "event-time"));
  card.append(textNode("h3", exam ? item.name : item.course_name, "event-title"));
  if (exam) {
    card.append(textNode("p", item.type || "考试", "event-meta"));
  } else {
    const periods = (item.periods || []).join("、");
    card.append(textNode("p", `第 ${periods} 节${item.source === "override" ? " · 调课/补课" : ""}`, "event-meta"));
    if (item.teachers?.length) card.append(textNode("p", item.teachers.join("、"), "event-meta"));
  }
  card.append(textNode("p", item.location || "地点待定", "event-location"));
  return card;
}

function renderWeek(data) {
  elements.title.textContent = data.title || "查看课程与考试安排";
  elements.week.textContent = `${readableDate(data.week_start)} — ${readableDate(data.week_end)}`;
  elements.number.textContent = data.week_number ? `第 ${data.week_number} 周` : "学期外";
  elements.date.value = selectedDate;
  elements.grid.replaceChildren();

  for (const day of data.days) {
    const column = document.createElement("section");
    column.className = "day";
    if (day.date === formatDate(new Date())) column.classList.add("today");
    const header = document.createElement("div");
    header.className = "day-header";
    header.append(textNode("span", `周${day.weekday}`, "day-name"));
    header.append(textNode("span", readableDate(day.date), "day-date"));
    column.append(header);

    const items = [
      ...day.courses.map((item) => ({ ...item, kind: "course" })),
      ...day.exams.map((item) => ({ ...item, kind: "exam" })),
    ].sort((left, right) => String(left.start).localeCompare(String(right.start)));
    if (!items.length) column.append(textNode("p", "暂无安排", "empty-day"));
    for (const item of items) column.append(eventCard(item));
    elements.grid.append(column);
  }
}

async function loadWeek() {
  const user = elements.user.value;
  if (!user) return;
  const currentRequest = ++requestNumber;
  setStatus("正在加载课表…");
  try {
    const data = await bridge.apiGet("week", { user, date: selectedDate });
    if (currentRequest !== requestNumber) return;
    renderWeek(data);
    setStatus("");
  } catch (error) {
    if (currentRequest !== requestNumber) return;
    elements.grid.replaceChildren();
    setStatus(`加载失败：${error.message}`, true);
  }
}

document.getElementById("previous-week").addEventListener("click", () => {
  selectedDate = shiftDate(selectedDate, -7);
  loadWeek();
});
document.getElementById("next-week").addEventListener("click", () => {
  selectedDate = shiftDate(selectedDate, 7);
  loadWeek();
});
document.getElementById("today").addEventListener("click", () => {
  selectedDate = formatDate(new Date());
  loadWeek();
});
elements.date.addEventListener("change", () => {
  if (!elements.date.value) return;
  selectedDate = elements.date.value;
  loadWeek();
});
elements.user.addEventListener("change", loadWeek);

try {
  await bridge.ready();
  const { users } = await bridge.apiGet("users");
  for (const user of users) {
    const option = document.createElement("option");
    option.value = user;
    option.textContent = user;
    elements.user.append(option);
  }
  elements.user.disabled = users.length === 0;
  if (users.length) {
    await loadWeek();
  } else {
    elements.week.textContent = "暂无课表";
    setStatus("还没有用户导入课表。可先在聊天中使用 /课表 导入示例。");
  }
} catch (error) {
  elements.week.textContent = "加载失败";
  setStatus(`无法打开周课表：${error.message}`, true);
}
