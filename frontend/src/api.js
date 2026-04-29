const API_BASE = '';

function getToken() {
  return localStorage.getItem('token') || '';
}

export function setAuthToken(token) {
  localStorage.setItem('token', token);
}

export function clearAuthToken() {
  localStorage.removeItem('token');
}

export function getAuthToken() {
  return getToken();
}

async function request(url, options = {}) {
  const headers = {
    ...(options.headers || {}),
  };

  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }

  const resp = await fetch(`${API_BASE}${url}`, {
    ...options,
    headers,
  });

  if (!resp.ok) {
    let msg = `请求失败: ${resp.status}`;
    try {
      const data = await resp.json();
      msg = data.detail || msg;
    } catch {}
    throw new Error(msg);
  }

  const contentType = resp.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return resp.json();
  }
  return resp.text();
}

export async function login(username, password, role) {
  return request('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password, role }),
  });
}

export async function registerUser(payload) {
  return request('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getForgotPasswordQuestion(username) {
  return request(`/api/auth/forgot-password/question?username=${encodeURIComponent(username)}`);
}

export async function resetForgotPassword(payload) {
  return request('/api/auth/forgot-password/reset', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function logout() {
  return request('/api/auth/logout', { method: 'POST' });
}

export async function getMe() {
  return request('/api/auth/me');
}

export async function getUsers() {
  return request('/api/admin/users');
}

export async function addUser(payload) {
  return request('/api/admin/users', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function deleteUser(username) {
  return request(`/api/admin/users/${encodeURIComponent(username)}`, {
    method: 'DELETE',
  });
}

export async function updateUserRole(username, role) {
  return request(`/api/admin/users/${encodeURIComponent(username)}/role`, {
    method: 'PUT',
    body: JSON.stringify({ role }),
  });
}

export async function updateUserEnabled(username, enabled) {
  return request(`/api/admin/users/${encodeURIComponent(username)}/enabled`, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  });
}

export async function adminResetPassword(username, new_password) {
  return request(`/api/admin/users/${encodeURIComponent(username)}/password`, {
    method: 'PUT',
    body: JSON.stringify({ new_password }),
  });
}

export async function getModules() {
  return request('/api/modules');
}

export async function saveModule(payload) {
  return request('/api/admin/modules', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function deleteModule(moduleId) {
  return request(`/api/admin/modules/${encodeURIComponent(moduleId)}`, {
    method: 'DELETE',
  });
}

export async function uploadModuleZip(file) {
  const fd = new FormData();
  fd.append('file', file);
  return request('/api/admin/modules/upload', {
    method: 'POST',
    body: fd,
  });
}

export async function getTasks() {
  return request('/api/tasks');
}

export async function getTask(taskId) {
  return request(`/api/tasks/${taskId}`);
}

export async function runModule(moduleId, inputs) {
  return request('/api/tasks/run', {
    method: 'POST',
    body: JSON.stringify({ module_id: moduleId, inputs }),
  });
}

export async function cancelTask(taskId) {
  return request(`/api/tasks/${taskId}/cancel`, {
    method: 'POST',
  });
}

export async function deleteTask(taskId) {
  return request(`/api/tasks/${taskId}`, {
    method: 'DELETE',
  });
}

/* 下面三个如果你本地浏览按钮已经接好，就保留；如果后端没有这些接口，可以继续用你原来的 */
export async function chooseLocalFile() {
  return request('/api/local/file', { method: 'POST' });
}

export async function chooseLocalDir() {
  return request('/api/local/dir', { method: 'POST' });
}

export async function chooseSaveFile() {
  return request('/api/local/save-file', { method: 'POST' });
}

export async function listUserFiles() {
  return request('/api/files');
}

export async function uploadUserFile(file) {
  const fd = new FormData();
  fd.append('file', file);
  return request('/api/files/upload', {
    method: 'POST',
    body: fd,
  });
}

export async function deleteUserFile(filename) {
  return request(`/api/files/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  });
}