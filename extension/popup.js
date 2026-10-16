/** 弹窗：显示桥接连接状态与最近一次应用的改动 */
const $ = (id) => document.getElementById(id);

function render(report) {
  if (!report) {
    $('online').textContent = '无响应';
    $('online').className = 'bad';
    return;
  }
  if (report.online) {
    $('online').textContent = '已连接';
    $('online').className = 'ok';
  } else {
    $('online').textContent = '未连接';
    $('online').className = 'bad';
  }
  $('at').textContent = report.at ? new Date(report.at).toLocaleTimeString() : '-';

  const d = report.delta || {};
  const r = report.result || {};
  const parts = [];
  if (d.add || d.remove || d.rename) {
    parts.push(`待应用：新增 ${d.add || 0} / 删除 ${d.remove || 0} / 改名 ${d.rename || 0}`);
  }
  if (r.added || r.removed || r.renamed) {
    parts.push(`本次已应用：新增 ${r.added || 0} / 删除 ${r.removed || 0} / 改名 ${r.renamed || 0}`);
  }
  if (!parts.length) parts.push(report.online ? '暂无需要应用的改动' : '请在终端启动 bmsync watch');
  if (report.error) parts.push('错误：' + report.error);
  if (r.errors && r.errors.length) {
    parts.push('部分失败：<ul>' + r.errors.slice(0, 5).map(e => `<li>${e}</li>`).join('') + '</ul>');
  }
  $('detail').innerHTML = parts.join('<br>');
}

chrome.runtime.sendMessage({ type: 'status' }, (resp) => {
  if (chrome.runtime.lastError) {
    render(null);
    return;
  }
  render(resp);
});
