const form = document.getElementById('task-form');
const status = document.getElementById('status');
const result = document.getElementById('result');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = form.querySelector('button');
  if (button.disabled) return;
  button.disabled = true;
  status.textContent = '正在验证并解析任务，请等待，不要重复提交……';
  result.textContent = '';
  const data = Object.fromEntries(new FormData(form));
  try {
    const response = await fetch('/api/tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const body = await response.json();
    status.textContent = body.message;
    if (body.result) result.textContent = JSON.stringify(body.result, null, 2);
    if (response.ok) form.elements.task.value = '';
  } catch (_error) {
    status.textContent = '连接中断，请联系管理员确认任务是否已收到，再决定是否重试。';
  } finally {
    form.elements.password.value = '';
    button.disabled = false;
  }
});
