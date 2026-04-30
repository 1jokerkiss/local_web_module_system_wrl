import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  login,
  registerUser,
  getForgotPasswordQuestion,
  resetForgotPassword,
  logout,
  getMe,
  getUsers,
  addUser,
  deleteUser,
  updateUserRole,
  updateUserEnabled,
  adminResetPassword,
  getModules,
  getAdminModules,
  getToolbars,
  addToolbar,
  updateToolbar,
  deleteToolbar,
  getTasks,
  runModule,
  saveModule,
  deleteModule as deleteModuleApi,
  uploadModuleZip,
  listDropZips,
  installLocalDropModules,
  getTask,
  cancelTask,
  deleteTask,
  chooseLocalFile,
  chooseLocalDir,
  chooseSaveFile,
  setAuthToken,
  clearAuthToken,
  getAuthToken,
  uploadUserFile,
  deleteUserFile,
    listUserFiles,
} from './api';


const defaultParallelConfig = {
  mode: 'auto',
  input_key: '',
  output_key: '',
  file_patterns: '*.tif;*.tiff;*.nc;*.hdf;*.h5',
  output_suffix: '.tif',
};

const emptyModuleForm = {
  id: '',
  name: '',
  description: '',
  executable: '',
  working_dir: '.',
  config_mode: 'none',
  command_template_text: '["{executable}"]',
  inputs_text: '[]',
  tags_text: '',
  tool_type: '',
  parallel_json_text: JSON.stringify(defaultParallelConfig, null, 2),
  extra_json_text: '{}',
  enabled: true,
};

const styles = {
  page: {
    minHeight: '100vh',
    background: 'linear-gradient(180deg, #eef4fa 0%, #e7f0f8 100%)',
    color: '#113459',
  },
  topbar: {
    height: 74,
    background: 'linear-gradient(135deg, #0b315a 0%, #12487f 55%, #1a67b6 100%)',
    color: '#fff',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0 18px',
    boxShadow: '0 8px 22px rgba(7,39,76,0.22)',
  },
  topBtn: {
    border: '1px solid rgba(255,255,255,0.25)',
    background: 'rgba(255,255,255,0.08)',
    color: '#fff',
    borderRadius: 10,
    padding: '10px 14px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  topBtnActive: {
    border: 'none',
    background: 'linear-gradient(135deg, #4aa2ff 0%, #2d7cf6 100%)',
    color: '#fff',
    borderRadius: 10,
    padding: '10px 14px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  blueBtn: {
    border: 'none',
    background: 'linear-gradient(135deg, #2d7cf6 0%, #235ed8 100%)',
    color: '#fff',
    borderRadius: 10,
    padding: '10px 16px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  whiteBtn: {
    border: '1px solid #cdd8ea',
    background: '#fff',
    color: '#17406b',
    borderRadius: 10,
    padding: '10px 16px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  redBtn: {
    border: 'none',
    background: 'linear-gradient(135deg, #df4b4b 0%, #c53232 100%)',
    color: '#fff',
    borderRadius: 10,
    padding: '10px 16px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  card: {
    background: 'rgba(248,251,255,0.98)',
    borderRadius: 18,
    border: '1px solid rgba(208,225,241,0.95)',
    boxShadow: '0 10px 24px rgba(8,34,70,0.08)',
  },
  input: {
    width: '100%',
    minHeight: 44,
    borderRadius: 10,
    border: '1px solid #d2dfec',
    padding: '0 12px',
    fontSize: 14,
    boxSizing: 'border-box',
    background: '#fff',
  },
  textarea: {
    width: '100%',
    minHeight: 90,
    borderRadius: 10,
    border: '1px solid #d2dfec',
    padding: '10px 12px',
    fontSize: 14,
    boxSizing: 'border-box',
    background: '#fff',
  },
};

function normalize(v) {
  return String(v || '').toLowerCase();
}

// 默认工具栏由后端首次初始化 toolbars.json 时提供。
// 前端不再强制追加 cloud/aerosol，避免删除后又在页面上复活。
const DEFAULT_TOOLBARS = [];
const ACTIVE_TAB_STORAGE_KEY = 'local_web_active_tab';

function getSavedActiveTab() {
  try {
    return localStorage.getItem(ACTIVE_TAB_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function saveActiveTab(tab) {
  try {
    if (tab) {
      localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, tab);
    }
  } catch {}
}

function clearSavedActiveTab() {
  try {
    localStorage.removeItem(ACTIVE_TAB_STORAGE_KEY);
  } catch {}
}
function normalizeToolKey(v) {
  return String(v || '')
    .trim()
    .replace(/\.\./g, '_')
    .replace(/[\\/\s]+/g, '_');
}

function guessToolType(module) {
  const explicit = normalizeToolKey(module?.tool_type || module?.category || '');
  if (explicit) return explicit;

  const text = `${normalize(module?.id)} ${normalize(module?.name)} ${normalize(module?.description)} ${normalize((module?.tags || []).join(' '))}`;

  if (['aod', 'aerosol', '气溶胶', 'h8', 'polar', '偏振'].some((x) => text.includes(x))) {
    return 'aerosol';
  }
  if (['cloud', '云', 'cloud_type', 'cth'].some((x) => text.includes(x))) {
    return 'cloud';
  }
  return 'cloud';
}

function getModuleToolType(module) {
  return guessToolType(module);
}

function getModuleParallelConfig(module) {
  const raw = module?.parallel && typeof module.parallel === 'object' ? module.parallel : {};
  return {
    mode: raw.mode || module?.parallel_mode || 'auto',
    input_key: raw.input_key || module?.parallel_input_key || '',
    output_key: raw.output_key || module?.parallel_output_key || '',
    file_patterns: raw.file_patterns || module?.parallel_file_patterns || '*.tif;*.tiff;*.nc;*.hdf;*.h5',
    output_suffix: raw.output_suffix || module?.parallel_output_suffix || '.tif',
  };
}

function isFieldVisibleToUser(field) {
  return field?.visible_to_user !== false && field?.admin_fixed !== true;
}

function makeEmptyInputField() {
  return {
    key: '',
    label: '',
    type: 'file_path',
    required: true,
    placeholder: '',
    default: '',
    help_text: '',
    visible_to_user: true,
    admin_fixed: false,
    path_mode: 'absolute',
    batch_role: '',
    match_mode: 'none',
  };
}

function pickModuleExtraFields(module) {
  const managed = new Set([
    'id', 'name', 'description', 'executable', 'working_dir', 'config_mode',
    'command_template', 'inputs', 'tags', 'tool_type', 'category', 'parallel',
    'parallel_mode', 'parallel_input_key', 'parallel_output_key', 'parallel_file_patterns',
    'parallel_output_suffix', 'enabled',
  ]);
  const extra = {};
  Object.entries(module || {}).forEach(([key, value]) => {
    if (!managed.has(key)) extra[key] = value;
  });
  return extra;
}

function uniqToolbars(toolbars, modules) {
  const map = new Map();
  (toolbars || []).forEach((t) => {
    const key = normalizeToolKey(t.key || t.label);
    if (key) map.set(key, { key, label: t.label || key, system: !!t.system });
  });
  (modules || []).forEach((m) => {
    const key = getModuleToolType(m);
    if (key && !map.has(key)) map.set(key, { key, label: key, system: false });
  });
  return Array.from(map.values()).sort((a, b) => {
    const aw = a.key === 'cloud' ? 0 : a.key === 'aerosol' ? 1 : 2;
    const bw = b.key === 'cloud' ? 0 : b.key === 'aerosol' ? 1 : 2;
    if (aw !== bw) return aw - bw;
    return String(a.label).localeCompare(String(b.label), 'zh-CN');
  });
}

function guessModuleByKeywords(modules, keywords) {
  return (
    modules.find((m) => {
      const text = `${normalize(m.id)} ${normalize(m.name)} ${normalize(
        m.description
      )} ${normalize((m.tags || []).join(' '))}`;
      return keywords.some((k) => text.includes(normalize(k)));
    }) || null
  );
}

function statusBadge(status) {
  let bg = '#e6eef8';
  let color = '#2d5177';

  if (status === 'success') {
    bg = '#daf5df';
    color = '#1f7f36';
  } else if (status === 'failed') {
    bg = '#f9dbdb';
    color = '#bb2c2c';
  } else if (status === 'running') {
    bg = '#ddecff';
    color = '#185cbc';
  } else if (status === 'queued') {
    bg = '#efe8ff';
    color = '#6e47be';
  }

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '6px 12px',
        borderRadius: 999,
        background: bg,
        color,
        fontWeight: 800,
        fontSize: 13,
      }}
    >
      {status}
    </span>
  );
}

function RunningDots({ active }) {
  const [dots, setDots] = useState('');
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      setDots((prev) => (prev.length >= 3 ? '' : prev + '.'));
    }, 450);
    return () => clearInterval(timer);
  }, [active]);
  return <span>{dots}</span>;
}

function SimpleOverlay({ title, onClose, children, width = 'min(960px, 96vw)' }) {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(7,22,44,0.32)',
        zIndex: 7000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 12,
      }}
    >
      <div
        style={{
          width,
          maxHeight: '94vh',
          overflow: 'auto',
          borderRadius: 18,
          background: 'rgba(248,251,255,0.98)',
          boxShadow: '0 22px 60px rgba(0,0,0,0.22)',
          padding: 18,
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 16,
          }}
        >
          <div style={{ fontSize: 22, fontWeight: 900, color: '#102a4a' }}>{title}</div>
          <button style={styles.whiteBtn} onClick={onClose}>
            关闭
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function TaskWindow({ win, onMin, onClose, onFront, onMove, onStop }) {
  const dragRef = useRef(null);
  const task = win.task;
  const running = task && ['queued', 'running'].includes(task.status);

  function onMouseDown(e) {
    if (e.button !== 0) return;
    onFront(win.id);
    dragRef.current = {
      x: e.clientX,
      y: e.clientY,
      left: win.left,
      top: win.top,
    };

    function onMoveDoc(ev) {
      if (!dragRef.current) return;
      const dx = ev.clientX - dragRef.current.x;
      const dy = ev.clientY - dragRef.current.y;
      onMove(win.id, dragRef.current.left + dx, dragRef.current.top + dy);
    }

    function onUpDoc() {
      dragRef.current = null;
      document.removeEventListener('mousemove', onMoveDoc);
      document.removeEventListener('mouseup', onUpDoc);
    }

    document.addEventListener('mousemove', onMoveDoc);
    document.addEventListener('mouseup', onUpDoc);
  }

  return (
    <div
      style={{
        position: 'fixed',
        left: win.left,
        top: win.top,
        width: 420,
        zIndex: win.zIndex,
        borderRadius: 16,
        overflow: 'hidden',
        boxShadow: '0 18px 46px rgba(5,25,55,0.28)',
        background: 'rgba(245,250,255,0.98)',
        border: '1px solid rgba(255,255,255,0.35)',
      }}
    >
      <div
        onMouseDown={onMouseDown}
        style={{
          cursor: 'move',
          background: 'linear-gradient(135deg,#0d4f92 0%,#1565c0 50%,#2c8ae8 100%)',
          color: '#fff',
          padding: '12px 14px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <div style={{ fontWeight: 800 }}>{win.title}</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button style={{ ...styles.topBtn, padding: '6px 10px' }} onClick={() => onMin(win.id)}>
            最小化
          </button>
          <button style={{ ...styles.topBtn, padding: '6px 10px' }} onClick={() => onClose(win.id)}>
            关闭
          </button>
        </div>
      </div>

      <div style={{ padding: 16 }}>
        <div
          style={{
            padding: 12,
            borderRadius: 12,
            background: 'linear-gradient(135deg, rgba(25,118,210,0.10), rgba(54,162,235,0.08))',
            border: '1px solid rgba(39,110,188,0.14)',
          }}
        >
          <div style={{ fontSize: 13, color: '#5f7088' }}>当前状态</div>
          <div style={{ fontSize: 20, fontWeight: 800, marginTop: 8 }}>
            {task?.status || '加载中'}
            {running && <RunningDots active={true} />}
          </div>
        </div>

        <div style={{ marginTop: 12, fontSize: 14, lineHeight: 1.7 }}>
          <div><strong>任务ID：</strong>{task?.id || '-'}</div>
          <div><strong>模块：</strong>{task?.module_name || '-'}</div>
          <div><strong>PID：</strong>{task?.pid || '-'}</div>
        </div>

        <div style={{ marginTop: 12 }}>
          <div style={{ fontWeight: 700, marginBottom: 8 }}>运行日志</div>
          <div
            style={{
              background: '#0a1730',
              color: '#dfe9ff',
              borderRadius: 12,
              padding: 12,
              minHeight: 84,
              maxHeight: 180,
              overflow: 'auto',
              fontSize: 12,
              whiteSpace: 'pre-wrap',
            }}
          >
            {task?.logs?.length ? task.logs.join('\n') : '暂无日志'}
          </div>
        </div>

        {running && (
          <div style={{ marginTop: 12 }}>
            <button style={styles.redBtn} onClick={() => onStop(win.id)}>
              停止任务
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function LoginPage(props) {
  const {
    authMode,
    setAuthMode,
    loginType,
    setLoginType,
    loginForm,
    setLoginForm,
    registerForm,
    setRegisterForm,
    forgotForm,
    setForgotForm,
    loginError,
    handleLogin,
    handleRegister,
    handleForgotQuestion,
    handleForgotReset,
  } = props;

  const [showPassword, setShowPassword] = useState(false);
  const [showRegisterPassword, setShowRegisterPassword] = useState(false);
  const [showRegisterConfirmPassword, setShowRegisterConfirmPassword] = useState(false);
  const [showForgotPassword, setShowForgotPassword] = useState(false);

  const outerCardStyle = {
    width: 'min(1050px, 96vw)',
    minHeight: 620,
    display: 'grid',
    gridTemplateColumns: '1.05fr 0.95fr',
    borderRadius: 24,
    overflow: 'hidden',
    boxShadow: '0 24px 80px rgba(0,0,0,0.28)',
    background: 'rgba(255,255,255,0.08)',
    border: '1px solid rgba(255,255,255,0.12)',
  };

  const innerFormCard = {
    width: '100%',
    maxWidth: 380,
    background: '#fff',
    borderRadius: 18,
    padding: '24px 26px 22px',
    boxShadow: '0 10px 30px rgba(25, 56, 120, 0.08)',
    border: '1px solid #eef2f7',
  };

  const fieldWrap = {
    display: 'flex',
    alignItems: 'center',
    minHeight: 44,
    border: '1px solid #cfd8e6',
    borderRadius: 8,
    padding: '0 14px',
    background: '#fff',
  };

  const fieldInput = {
    flex: 1,
    border: 'none',
    outline: 'none',
    fontSize: 15,
    height: 40,
    background: 'transparent',
    color: '#22324a',
  };

  const suffixText = {
    color: '#6e8097',
    fontSize: 14,
    marginLeft: 10,
    whiteSpace: 'nowrap',
  };

  const linkBtn = {
    border: 'none',
    background: 'transparent',
    color: '#4a78e8',
    cursor: 'pointer',
    fontSize: 14,
    padding: 0,
  };

  const roleBtn = {
    border: '1px solid #d8e1ef',
    background: '#fff',
    color: '#173353',
    borderRadius: 10,
    padding: '12px 0',
    fontWeight: 700,
    cursor: 'pointer',
  };

  const roleBtnActive = {
    ...roleBtn,
    border: '1px solid #4a84ff',
    background: '#eef4ff',
    color: '#235ed8',
  };

  const titleMap = {
    login: '账号登录',
    register: '账号注册',
    forgot: '找回密码',
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background:
          'radial-gradient(circle at 20% 20%, rgba(78,134,255,0.35), transparent 28%), radial-gradient(circle at 80% 30%, rgba(0,197,255,0.24), transparent 24%), linear-gradient(135deg, #0a2d57 0%, #0b2c50 35%, #0d3d69 70%, #0b3158 100%)',
        padding: 20,
      }}
    >
      <div style={outerCardStyle}>
        {/* 左侧介绍区 */}
        <div
          style={{
            padding: '48px 42px',
            color: '#fff',
            background: 'linear-gradient(180deg, rgba(3,18,38,0.78), rgba(8,32,60,0.72))',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
          }}
        >
          <div>
            <div
              style={{
                display: 'inline-flex',
                padding: '8px 14px',
                borderRadius: 999,
                background: 'rgba(255,255,255,0.10)',
                fontSize: 14,
                marginBottom: 28,
              }}
            >
              遥感反演 · 本地运行平台
            </div>

            <h1 style={{ fontSize: 42, lineHeight: 1.25, margin: 0, fontWeight: 800 }}>
              云和气溶胶反演系统
            </h1>

            <p
              style={{
                marginTop: 22,
                fontSize: 18,
                lineHeight: 1.9,
                color: 'rgba(255,255,255,0.86)',
              }}
            >
              面向遥感业务场景的本地模块化运行平台，支持云检测、
              气溶胶反演、模块接入、任务并行调度与结果追踪。
            </p>
          </div>

          <div
            style={{
              display: 'flex',
              gap: 14,
              flexWrap: 'wrap',
              color: 'rgba(255,255,255,0.78)',
              fontSize: 14,
            }}
          >
            <span>H8</span>
            <span>FY</span>
            <span>AOD</span>
            <span>Cloud Mask</span>
            <span>Remote Sensing</span>
          </div>
        </div>

        {/* 右侧登录区域 */}
        <div
          style={{
            background: 'rgba(248,251,255,0.95)',
            padding: '52px 42px',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div style={{ width: '100%', maxWidth: 420 }}>
            <div style={{ marginBottom: 18 }}>
              <h2
                style={{
                  margin: 0,
                  fontSize: 28,
                  fontWeight: 800,
                  color: '#10233f',
                }}
              >
                欢迎进入系统
              </h2>
            </div>

            <div style={innerFormCard}>
              {authMode !== 'login' && (
                <div style={{ marginBottom: 10 }}>
                  <button style={linkBtn} onClick={() => setAuthMode('login')}>
                    返回登录
                  </button>
                </div>
              )}

              <div
                style={{
                  textAlign: 'center',
                  fontSize: 18,
                  fontWeight: 800,
                  color: '#111',
                  marginBottom: 22,
                }}
              >
                {titleMap[authMode]}
              </div>

              {/* 登录 */}
              {authMode === 'login' && (
                <>
                  <div style={fieldWrap}>
                    <input
                      value={loginForm.username}
                      onChange={(e) =>
                        setLoginForm({ ...loginForm, username: e.target.value })
                      }
                      placeholder="请输入用户名"
                      style={fieldInput}
                    />
                    <span style={suffixText}>账号</span>
                  </div>

                  <div style={{ ...fieldWrap, marginTop: 14 }}>
                    <input
                      type={showPassword ? 'text' : 'password'}
                      value={loginForm.password}
                      onChange={(e) =>
                        setLoginForm({ ...loginForm, password: e.target.value })
                      }
                      placeholder="输入密码"
                      style={fieldInput}
                    />
                    <button
                      type="button"
                      style={{ ...linkBtn, color: '#8fa0b4' }}
                      onClick={() => setShowPassword((v) => !v)}
                    >
                      {showPassword ? '隐藏' : '显示'}
                    </button>
                  </div>

                  <div
                      style={{
                        marginTop: 12,
                        display: 'flex',
                        justifyContent: 'flex-end',
                        alignItems: 'center',
                        fontSize: 14,
                        color: '#5f7088',
                      }}
                    >
                      <button
                        type="button"
                        style={linkBtn}
                        onClick={() => setAuthMode('forgot')}
                      >
                        忘记密码
                      </button>
                    </div>

                  <div
                    style={{
                      marginTop: 16,
                      display: 'grid',
                      gridTemplateColumns: '1fr 1fr',
                      gap: 12,
                    }}
                  >
                    <button
                      type="button"
                      style={loginType === 'user' ? roleBtnActive : roleBtn}
                      onClick={() => setLoginType('user')}
                    >
                      用户
                    </button>

                    <button
                      type="button"
                      style={loginType === 'admin' ? roleBtnActive : roleBtn}
                      onClick={() => setLoginType('admin')}
                    >
                      管理员
                    </button>
                  </div>

                  <button
                    style={{ ...widePrimaryBtn, marginTop: 20 }}
                    onClick={handleLogin}
                  >
                    登 录
                  </button>

                  <div style={{ textAlign: 'center', marginTop: 14 }}>
                    <button
                      type="button"
                      style={linkBtn}
                      onClick={() => setAuthMode('register')}
                    >
                      注册新账号
                    </button>
                  </div>
                </>
              )}

              {/* 注册 */}
              {authMode === 'register' && (
                <>
                  <div style={{ display: 'grid', gap: 12 }}>
                    <div style={fieldWrap}>
                      <input
                        value={registerForm.username}
                        onChange={(e) =>
                          setRegisterForm({ ...registerForm, username: e.target.value })
                        }
                        placeholder="请输入用户名"
                        style={fieldInput}
                      />
                    </div>

                    <div style={fieldWrap}>
                      <input
                        type={showRegisterPassword ? 'text' : 'password'}
                        value={registerForm.password}
                        onChange={(e) =>
                          setRegisterForm({ ...registerForm, password: e.target.value })
                        }
                        placeholder="请输入密码"
                        style={fieldInput}
                      />
                      <button
                        type="button"
                        style={{ ...linkBtn, color: '#8fa0b4' }}
                        onClick={() => setShowRegisterPassword((v) => !v)}
                      >
                        {showRegisterPassword ? '隐藏' : '显示'}
                      </button>
                    </div>

                    <div style={fieldWrap}>
                      <input
                        type={showRegisterConfirmPassword ? 'text' : 'password'}
                        value={registerForm.confirm_password}
                        onChange={(e) =>
                          setRegisterForm({
                            ...registerForm,
                            confirm_password: e.target.value,
                          })
                        }
                        placeholder="请输入确认密码"
                        style={fieldInput}
                      />
                      <button
                        type="button"
                        style={{ ...linkBtn, color: '#8fa0b4' }}
                        onClick={() => setShowRegisterConfirmPassword((v) => !v)}
                      >
                        {showRegisterConfirmPassword ? '隐藏' : '显示'}
                      </button>
                    </div>

                    <div style={fieldWrap}>
                      <input
                        value={registerForm.security_question}
                        onChange={(e) =>
                          setRegisterForm({
                            ...registerForm,
                            security_question: e.target.value,
                          })
                        }
                        placeholder="请输入安全问题"
                        style={fieldInput}
                      />
                    </div>

                    <div style={fieldWrap}>
                      <input
                        value={registerForm.security_answer}
                        onChange={(e) =>
                          setRegisterForm({
                            ...registerForm,
                            security_answer: e.target.value,
                          })
                        }
                        placeholder="请输入安全答案"
                        style={fieldInput}
                      />
                    </div>
                  </div>

                  <button
                    style={{ ...widePrimaryBtn, marginTop: 20 }}
                    onClick={handleRegister}
                  >
                    注 册
                  </button>
                </>
              )}

              {/* 找回密码 */}
              {authMode === 'forgot' && (
                <>
                  <div style={{ display: 'grid', gap: 12 }}>
                    <div style={fieldWrap}>
                      <input
                        value={forgotForm.username}
                        onChange={(e) =>
                          setForgotForm({ ...forgotForm, username: e.target.value })
                        }
                        placeholder="请输入用户名"
                        style={fieldInput}
                      />
                    </div>

                    <button
                      style={{ ...styles.whiteBtn, width: '100%' }}
                      onClick={handleForgotQuestion}
                    >
                      获取安全问题
                    </button>

                    <div style={fieldWrap}>
                      <input
                        value={forgotForm.question}
                        readOnly
                        placeholder="安全问题"
                        style={fieldInput}
                      />
                    </div>

                    <div style={fieldWrap}>
                      <input
                        value={forgotForm.answer}
                        onChange={(e) =>
                          setForgotForm({ ...forgotForm, answer: e.target.value })
                        }
                        placeholder="请输入安全答案"
                        style={fieldInput}
                      />
                    </div>

                    <div style={fieldWrap}>
                      <input
                        type={showForgotPassword ? 'text' : 'password'}
                        value={forgotForm.new_password}
                        onChange={(e) =>
                          setForgotForm({
                            ...forgotForm,
                            new_password: e.target.value,
                          })
                        }
                        placeholder="请输入新密码"
                        style={fieldInput}
                      />
                      <button
                        type="button"
                        style={{ ...linkBtn, color: '#8fa0b4' }}
                        onClick={() => setShowForgotPassword((v) => !v)}
                      >
                        {showForgotPassword ? '隐藏' : '显示'}
                      </button>
                    </div>
                  </div>

                  <button
                    style={{ ...widePrimaryBtn, marginTop: 20 }}
                    onClick={handleForgotReset}
                  >
                    重置密码
                  </button>
                </>
              )}

              {loginError && (
                <div
                  style={{
                    marginTop: 16,
                    padding: '10px 12px',
                    borderRadius: 10,
                    background: 'rgba(220,38,38,0.06)',
                    color: '#d43838',
                    fontSize: 13,
                    lineHeight: 1.6,
                  }}
                >
                  {loginError}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const typeCard = {
  flex: 1,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 8,
  padding: '14px 12px',
  borderRadius: 12,
  border: '1px solid #d7dfeb',
  background: '#fff',
  cursor: 'pointer',
  color: '#173353',
  fontWeight: 700,
};

const selectedTypeCard = {
  ...typeCard,
  border: '2px solid #3b82f6',
  background: 'rgba(59,130,246,0.08)',
};

const widePrimaryBtn = {
  width: '100%',
  height: 48,
  fontSize: 16,
  fontWeight: 700,
  border: 'none',
  borderRadius: 12,
  cursor: 'pointer',
  color: '#fff',
  background: 'linear-gradient(135deg, #2d7cf6 0%, #235ed8 100%)',
};

const labelStyle = {
  marginBottom: 8,
  fontWeight: 700,
  color: '#173353',
};

export default function App() {
    const [userFiles, setUserFiles] = useState([]);
    const [uploadingFile, setUploadingFile] = useState(false);
    const [fileActionMode, setFileActionMode] = useState('');
    const fileInputRef = useRef(null);
  const [currentUser, setCurrentUser] = useState(null);
  const [authMode, setAuthMode] = useState('login');
  const [loginType, setLoginType] = useState('user');
  const [activeCloudId, setActiveCloudId] = useState('');
  const [activeAerosolId, setActiveAerosolId] = useState('');
  const [loginForm, setLoginForm] = useState({ username: '', password: '' });
  const [registerForm, setRegisterForm] = useState({
    username: '',
    password: '',
    confirm_password: '',
    security_question: '',
    security_answer: '',
  });
  const [forgotForm, setForgotForm] = useState({
    username: '',
    question: '',
    answer: '',
    new_password: '',
  });
  const [loginError, setLoginError] = useState('');
  const [startupError, setStartupError] = useState('');

  const [modules, setModules] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [users, setUsers] = useState([]);
  const [toolbars, setToolbars] = useState(DEFAULT_TOOLBARS);

  const [activeTab, setActiveTab] = useState(() => getSavedActiveTab() || 'tool:cloud');
  const [activeModuleByTool, setActiveModuleByTool] = useState({});
  const [expandedToolTypes, setExpandedToolTypes] = useState({ cloud: true, aerosol: true });
  const [cloudForms, setCloudForms] = useState({});

  const [runtimeForms, setRuntimeForms] = useState({});
  const [moduleForm, setModuleForm] = useState(emptyModuleForm);
  const [editingModuleId, setEditingModuleId] = useState('');
  const [inputEditorOpen, setInputEditorOpen] = useState(false);
  const [inputEditorFields, setInputEditorFields] = useState([]);
  const [zipFile, setZipFile] = useState(null);
  const [uploadToolType, setUploadToolType] = useState('');
  const [dropInfo, setDropInfo] = useState({ drop_dir: '', items: [] });
  const [uploadMsg, setUploadMsg] = useState('');
  const [newToolbarForm, setNewToolbarForm] = useState({ key: '', label: '' });
  const [editingToolbarKey, setEditingToolbarKey] = useState('');
  const [toolbarEditForm, setToolbarEditForm] = useState({ key: '', label: '' });
  const [newUserForm, setNewUserForm] = useState({
    username: '',
    password: '',
    role: 'user',
    security_question: '',
    security_answer: '',
  });
  const [showDropHint, setShowDropHint] = useState(false);

  const [windows, setWindows] = useState([]);
  const zRef = useRef(2000);
  const pollTimerRef = useRef(null);

  const isAdmin = currentUser?.role === 'admin';

  const visibleToolbars = useMemo(() => uniqToolbars(toolbars, modules), [toolbars, modules]);

  const modulesByTool = useMemo(() => {
    const grouped = {};
    visibleToolbars.forEach((t) => {
      grouped[t.key] = [];
    });
    modules.forEach((m) => {
      const key = getModuleToolType(m);
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(m);
    });
    Object.keys(grouped).forEach((key) => {
      grouped[key].sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id), 'zh-CN'));
    });
    return grouped;
  }, [modules, visibleToolbars]);

  const navItems = useMemo(() => {
    const arr = [];
    if (isAdmin) {
      arr.push({ key: 'module_mgmt', label: '模块管理' });
      arr.push({ key: 'user_mgmt', label: '用户管理' });
    }
    visibleToolbars.forEach((t) => arr.push({ key: `tool:${t.key}`, label: t.label }));
    arr.push({ key: 'tasks', label: '任务列表' });
    return arr;
  }, [isAdmin, visibleToolbars]);

  useEffect(() => {
    if (currentUser) {
      loadUserFiles();
    } else {
      setUserFiles([]);
    }
  }, [currentUser]);

  useEffect(() => {
    const init = async () => {
      if (!getAuthToken()) return;
      try {
        const me = await getMe();
        setCurrentUser(me);
        setActiveTab(getSavedActiveTab() || 'tool:cloud');

        const [toolbarList, mods, taskList] = await Promise.all([
          getToolbars(),
          me.role === 'admin' ? getAdminModules() : getModules(),
          getTasks(),
        ]);
        setToolbars(Array.isArray(toolbarList) ? toolbarList : DEFAULT_TOOLBARS);
        setModules(Array.isArray(mods) ? mods : []);
        setTasks(Array.isArray(taskList) ? taskList : []);

        if (me.role === 'admin') {
          const [userList, drop] = await Promise.all([getUsers(), listDropZips().catch(() => null)]);
          setUsers(Array.isArray(userList) ? userList : []);
          if (drop) setDropInfo(drop);
        }
      } catch (e) {
        clearAuthToken();
        setStartupError(e?.message || '系统初始化失败');
      }
    };
    init();
  }, []);
useEffect(() => {
  modules.forEach((m) => {
    setRuntimeForms((prev) => {
      if (prev[m.id]) return prev;
      const init = { task_name: m.name, _parallel_workers: 1 };
      (m.inputs || []).filter(isFieldVisibleToUser).forEach((f) => {
        init[f.key] = f.default ?? '';
      });
      return { ...prev, [m.id]: init };
    });
  });
}, [modules]);

useEffect(() => {
  setActiveModuleByTool((prev) => {
    const next = { ...prev };
    visibleToolbars.forEach((tb) => {
      const list = modulesByTool[tb.key] || [];
      if (!list.length) return;
      if (!next[tb.key] || !list.some((m) => m.id === next[tb.key])) {
        next[tb.key] = list[0].id;
      }
    });
    return next;
  });
}, [visibleToolbars, modulesByTool]);

useEffect(() => {
  if (!currentUser) return;

  // 工具栏还没加载完成时，不要急着把 tool:cloud 切到 tasks
  if (visibleToolbars.length === 0) return;

  const hasTool = (key) => visibleToolbars.some((tb) => tb.key === key);
  const firstKey = visibleToolbars[0]?.key || '';

  if (activeTab.startsWith('tool:')) {
    const key = activeTab.slice('tool:'.length);
    if (!hasTool(key)) {
      const fallback = hasTool('cloud')
        ? 'tool:cloud'
        : firstKey
          ? `tool:${firstKey}`
          : 'tasks';

      setActiveTab(fallback);
      saveActiveTab(fallback);
    }
  }

  if (!uploadToolType || !hasTool(uploadToolType)) {
    setUploadToolType(firstKey);
  }

  setModuleForm((prev) => {
    if (prev.tool_type && hasTool(prev.tool_type)) return prev;
    if (!firstKey) return prev;
    return { ...prev, tool_type: firstKey };
  });
}, [currentUser, visibleToolbars, activeTab, uploadToolType]);
  useEffect(() => {
    if (!currentUser) {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    const hasRunningTask =
      tasks.some((t) => t.status === 'queued' || t.status === 'running') ||
      windows.some((w) => {
        const s = w.task?.status;
        return s === 'queued' || s === 'running';
      });

    if (!hasRunningTask) {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }

    pollTimerRef.current = setInterval(async () => {
      try {
        const latestTasks = await getTasks();
        setTasks(Array.isArray(latestTasks) ? latestTasks : []);

        for (const w of windows) {
          if (!w.taskId) continue;
          try {
            const detail = await getTask(w.taskId);
            setWindows((prev) =>
              prev.map((x) => (x.id === w.id ? { ...x, task: detail } : x))
            );
          } catch {}
        }
      } catch {}
    }, 3000);

    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [currentUser, tasks, windows]);
useEffect(() => {
  if (!currentUser) return;
  saveActiveTab(activeTab);
}, [currentUser, activeTab]);
  async function handleLogin() {
    try {
      setLoginError('');
      const data = await login(loginForm.username, loginForm.password, loginType);
      setAuthToken(data.token);
      setCurrentUser(data.user);
      setActiveTab(data.user.role === 'admin' ? 'module_mgmt' : 'tool:cloud');

      const [toolbarList, mods, taskList] = await Promise.all([
        getToolbars(),
        data.user.role === 'admin' ? getAdminModules() : getModules(),
        getTasks(),
      ]);
      setToolbars(Array.isArray(toolbarList) ? toolbarList : DEFAULT_TOOLBARS);
      setModules(Array.isArray(mods) ? mods : []);
      setTasks(Array.isArray(taskList) ? taskList : []);

      if (data.user.role === 'admin') {
        const [userList, drop] = await Promise.all([getUsers(), listDropZips().catch(() => null)]);
        setUsers(Array.isArray(userList) ? userList : []);
        if (drop) setDropInfo(drop);
      }
    } catch (e) {
      setLoginError(e?.message || '登录失败，请检查账号、密码或登录身份是否匹配');
    }
  }

async function handleRegister() {
  try {
    setLoginError('');

    if (!registerForm.username.trim()) {
      setLoginError('请输入用户名');
      return;
    }

    if (!registerForm.password) {
      setLoginError('请输入密码');
      return;
    }

    if (!registerForm.confirm_password) {
      setLoginError('请输入确认密码');
      return;
    }

    if (registerForm.password !== registerForm.confirm_password) {
      setLoginError('两次输入的密码不一致');
      return;
    }

    await registerUser({
      username: registerForm.username,
      password: registerForm.password,
      security_question: registerForm.security_question,
      security_answer: registerForm.security_answer,
    });

    setRegisterForm({
      username: '',
      password: '',
      confirm_password: '',
      security_question: '',
      security_answer: '',
    });

    setAuthMode('login');
    alert('注册成功，请登录');
  } catch (e) {
    setLoginError(e?.message || '注册失败');
  }
}

  async function handleForgotQuestion() {
    try {
      const data = await getForgotPasswordQuestion(forgotForm.username);
      setForgotForm((p) => ({ ...p, question: data.question || '' }));
    } catch (e) {
      alert(e?.message || '获取安全问题失败');
    }
  }

  async function handleForgotReset() {
    try {
      await resetForgotPassword({
        username: forgotForm.username,
        answer: forgotForm.answer,
        new_password: forgotForm.new_password,
      });
      alert('密码已重置');
      setAuthMode('login');
    } catch (e) {
      alert(e?.message || '重置密码失败');
    }
  }
  async function handleLogout() {
    try {
      await logout();
    } catch {}

    clearAuthToken();
    clearSavedActiveTab();

    setCurrentUser(null);
    setActiveTab('tool:cloud');
    setModules([]);
    setTasks([]);
    setUsers([]);
    setWindows([]);
}

  async function refreshModules() {
    const list = isAdmin ? await getAdminModules() : await getModules();
    setModules(Array.isArray(list) ? list : []);
  }

  async function refreshToolbars() {
    const list = await getToolbars();
    const next = Array.isArray(list) ? list : DEFAULT_TOOLBARS;
    setToolbars(next);
    return next;
  }

  async function refreshDropZipList() {
    if (!isAdmin) return;
    try {
      const data = await listDropZips();
      setDropInfo(data || { drop_dir: '', items: [] });
    } catch {}
  }

  async function refreshUsers() {
    const list = await getUsers();
    setUsers(Array.isArray(list) ? list : []);
  }

  async function refreshTasks() {
    const list = await getTasks();
    setTasks(Array.isArray(list) ? list : []);
  }

  function addTaskWindow(task, title) {
    zRef.current += 1;
    setWindows((prev) => [
      ...prev,
      {
        id: `w_${task.id}`,
        taskId: task.id,
        task,
        title,
        minimized: false,
        left: 120 + (prev.length % 5) * 30,
        top: 90 + (prev.length % 4) * 28,
        zIndex: zRef.current,
      },
    ]);
  }

  function bringFront(id) {
    zRef.current += 1;
    setWindows((prev) => prev.map((x) => (x.id === id ? { ...x, zIndex: zRef.current } : x)));
  }

  function moveWindow(id, left, top) {
    setWindows((prev) => prev.map((x) => (x.id === id ? { ...x, left, top } : x)));
  }

  async function stopTaskWindow(id) {
    const target = windows.find((x) => x.id === id);
    if (!target) return;
    try {
      await cancelTask(target.taskId);
      await refreshTasks();
    } catch (e) {
      alert(e?.message || '停止任务失败');
    }
  }

  async function handleDeleteTask(taskId) {
    const ok = window.confirm(`确定删除任务 ${taskId} 吗？`);
    if (!ok) return;

    try {
      await deleteTask(taskId);
      setWindows((prev) => prev.filter((w) => w.taskId !== taskId));
      setTasks((prev) => prev.filter((t) => t.id !== taskId));
      const latestTasks = await getTasks();
      setTasks(Array.isArray(latestTasks) ? latestTasks : []);
    } catch (e) {
      alert(e?.message || '删除失败');
    }
  }

  async function browseCloud(key, field) {
    try {
      const result = await chooseLocalDir({
        title: field === 'output_dir' ? '选择输出文件夹' : '选择输入文件夹',
      });
      if (result?.path) {
        setCloudForms((prev) => ({
          ...prev,
          [key]: {
            ...prev[key],
            [field]: result.path,
          },
        }));
      }
    } catch (e) {
      alert(e?.message || '选择路径失败');
    }
  }

  async function browseField(module, field) {
    try {
      let result;
      const isOutput =
        normalize(field.key) === 'output' || String(field.label || '').includes('输出');

      if (field.type === 'dir_path') {
        result = await chooseLocalDir({ title: `选择${field.label || field.key}` });
      } else if (isOutput) {
        result = await chooseSaveFile({
          title: `选择${field.label || field.key}`,
          defaultextension: '.tif',
          filetypes: [['GeoTIFF', '*.tif'], ['All Files', '*.*']],
        });
      } else {
        result = await chooseLocalFile({
          title: `选择${field.label || field.key}`,
          filetypes: [['All Files', '*.*']],
        });
      }

      if (result?.path) {
        setRuntimeForms((prev) => ({
          ...prev,
          [module.id]: {
            ...prev[module.id],
            [field.key]: result.path,
          },
        }));
      }
    } catch (e) {
      alert(e?.message || '浏览失败');
    }
  }

  async function runCloud(item) {
    try {
      if (!item.module) {
        alert('未找到对应模块');
        return;
      }

      const form = cloudForms[item.key];
      const inputs = {};
      const inputField = (item.module.inputs || []).find((f) => normalize(f.key).includes('input'));
      const outputField = (item.module.inputs || []).find((f) => normalize(f.key).includes('output'));

      if (inputField) inputs[inputField.key] = form.input_path;
      if (outputField) inputs[outputField.key] = form.output_dir;

      const task = await runModule(item.module.id, inputs);
      const detail = await getTask(task.id);
      addTaskWindow(detail, form.task_name || item.title);
      await refreshTasks();
    } catch (e) {
      alert(e?.message || '运行失败');
    }
  }

  async function runGeneric(module) {
    try {
      if (!module) return;
      const form = runtimeForms[module.id] || {};
      const inputs = { ...form };
      const title = form.task_name || module.name;
      const parallelWorkers = Number(form._parallel_workers || 1);
      delete inputs.task_name;
      delete inputs._parallel_workers;

      const task = await runModule(module.id, inputs, parallelWorkers);
      const detail = await getTask(task.id);
      addTaskWindow(detail, title);
      await refreshTasks();
    } catch (e) {
      alert(e?.message || '运行失败');
    }
  }

  function fillModuleForm(module) {
    setEditingModuleId(module.id);
    setModuleForm({
      id: module.id || '',
      name: module.name || '',
      description: module.description || '',
      executable: module.executable || '',
      working_dir: module.working_dir || '.',
      config_mode: module.config_mode || 'none',
      command_template_text: JSON.stringify(module.command_template || ['{executable}'], null, 2),
      inputs_text: JSON.stringify(module.inputs || [], null, 2),
      tags_text: (module.tags || []).join(','),
      tool_type: getModuleToolType(module),
      parallel_json_text: JSON.stringify(getModuleParallelConfig(module), null, 2),
      extra_json_text: JSON.stringify(pickModuleExtraFields(module), null, 2),
      enabled: module.enabled !== false,
    });
  }

  async function saveCurrentModule() {
    try {
      const extraModuleFields = JSON.parse(moduleForm.extra_json_text || '{}');
      await saveModule({
        ...extraModuleFields,
        id: moduleForm.id.trim(),
        name: moduleForm.name.trim(),
        description: moduleForm.description,
        executable: moduleForm.executable,
        working_dir: moduleForm.working_dir,
        config_mode: moduleForm.config_mode,
        command_template: JSON.parse(moduleForm.command_template_text || '[]'),
        inputs: JSON.parse(moduleForm.inputs_text || '[]'),
        tags: moduleForm.tags_text
          .split(',')
          .map((x) => x.trim())
          .filter(Boolean),
        tool_type: moduleForm.tool_type || visibleToolbars[0]?.key || 'uncategorized',
        parallel: JSON.parse(moduleForm.parallel_json_text || '{}'),
        enabled: moduleForm.enabled,
      });
      setModuleForm(emptyModuleForm);
      setEditingModuleId('');
      await Promise.all([refreshModules(), refreshToolbars()]);
      alert('模块已保存');
    } catch (e) {
      alert(e?.message || '保存模块失败');
    }
  }

  async function uploadZip() {
    if (!zipFile) {
      setUploadMsg('未选择 zip 文件；如果已放入本地投放目录，请点“扫描本地目录安装”。');
      return;
    }
    if (!uploadToolType) {
      alert('请先添加或选择一个工具栏');
      return;
    }
    setUploadMsg('上传中...');
    try {
      await uploadModuleZip(zipFile, uploadToolType);
      setZipFile(null);
      setUploadMsg('上传并安装成功');
      await Promise.all([refreshModules(), refreshToolbars(), refreshDropZipList()]);
    } catch (e) {
      setUploadMsg(e?.message || '上传失败');
    }
  }

  async function installFromDrop(filename = '') {
    if (!uploadToolType) {
      alert('请先添加或选择一个工具栏');
      return;
    }
    setUploadMsg('正在扫描本地投放目录...');
    try {
      const data = await installLocalDropModules(uploadToolType, filename);
      const okCount = data?.installed?.length || 0;
      const failCount = data?.failed?.length || 0;
      setUploadMsg('本地目录安装完成：成功 ' + okCount + ' 个，失败 ' + failCount + ' 个');
      await Promise.all([refreshModules(), refreshToolbars(), refreshDropZipList()]);
      if (failCount) {
        alert((data.failed || []).map((x) => `${x.name}: ${x.error}`).join('\n'));
      }
    } catch (e) {
      setUploadMsg(e?.message || '本地目录安装失败');
    }
  }

  async function handleAddToolbar() {
    try {
      const label = newToolbarForm.label.trim();
      if (!label) {
        alert('请输入工具类型名称');
        return;
      }
      await addToolbar({
        key: normalizeToolKey(newToolbarForm.key || label),
        label,
      });
      setNewToolbarForm({ key: '', label: '' });
      const createdKey = normalizeToolKey(newToolbarForm.key || label);
      await refreshToolbars();
      if (!uploadToolType) setUploadToolType(createdKey);
      alert('工具栏已添加');
    } catch (e) {
      alert(e?.message || '添加工具栏失败');
    }
  }

  function startEditToolbar(toolbar) {
    setEditingToolbarKey(toolbar.key);
    setToolbarEditForm({ key: toolbar.key, label: toolbar.label || toolbar.key });
  }

  function cancelEditToolbar() {
    setEditingToolbarKey('');
    setToolbarEditForm({ key: '', label: '' });
  }

  async function handleUpdateToolbar() {
    try {
      const label = toolbarEditForm.label.trim();
      if (!editingToolbarKey || !label) {
        alert('请输入工具类型名称');
        return;
      }
      const data = await updateToolbar(editingToolbarKey, {
        key: normalizeToolKey(toolbarEditForm.key || label),
        label,
      });
      const updatedKey = data?.toolbar?.key || normalizeToolKey(toolbarEditForm.key || label);
      if (activeTab === `tool:${editingToolbarKey}`) {
        setActiveTab(`tool:${updatedKey}`);
      }
      if (uploadToolType === editingToolbarKey) {
        setUploadToolType(updatedKey);
      }
      setActiveModuleByTool((prev) => {
        if (updatedKey === editingToolbarKey || !prev[editingToolbarKey]) return prev;
        const next = { ...prev, [updatedKey]: prev[editingToolbarKey] };
        delete next[editingToolbarKey];
        return next;
      });
      setExpandedToolTypes((prev) => {
        if (updatedKey === editingToolbarKey) return prev;
        const next = { ...prev, [updatedKey]: prev[editingToolbarKey] };
        delete next[editingToolbarKey];
        return next;
      });
      cancelEditToolbar();
      await Promise.all([refreshToolbars(), refreshModules()]);
      alert('工具栏已更新');
    } catch (e) {
      alert(e?.message || '更新工具栏失败');
    }
  }

  async function handleDeleteToolbar(toolbar) {
    try {
      const list = modulesByTool[toolbar.key] || [];
      const extra = list.length > 0
        ? `\n该工具栏下有 ${list.length} 个模块，删除工具栏后这些模块会自动移动到其它工具栏；如果没有其它工具栏，会自动移动到“未分类”。`
        : '';
      if (!window.confirm(`确定删除工具栏「${toolbar.label || toolbar.key}」吗？${extra}`)) return;
      const data = await deleteToolbar(toolbar.key);
      const targetKey = data?.target_tool_type || '';
      const latestToolbars = await refreshToolbars();
      await refreshModules();

      if (activeTab === `tool:${toolbar.key}`) {
        const nextKey = targetKey || latestToolbars?.[0]?.key || '';
        setActiveTab(nextKey ? `tool:${nextKey}` : 'module_mgmt');
      }
      if (uploadToolType === toolbar.key) {
        const nextKey = targetKey || latestToolbars?.[0]?.key || '';
        setUploadToolType(nextKey);
      }
      if (editingToolbarKey === toolbar.key) {
        cancelEditToolbar();
      }
      if (data?.moved_count) {
        alert(`工具栏已删除，${data.moved_count} 个模块已移动到其它工具栏`);
      } else {
        alert('工具栏已删除');
      }
    } catch (e) {
      alert(e?.message || '删除工具栏失败');
    }
  }

  function openInputEditor() {
    try {
      const fields = JSON.parse(moduleForm.inputs_text || '[]');
      if (!Array.isArray(fields)) {
        alert('输入字段必须是 JSON 数组');
        return;
      }
      setInputEditorFields(fields.map((f) => ({ ...makeEmptyInputField(), ...f })));
      setInputEditorOpen(true);
    } catch (e) {
      alert('输入字段 JSON 格式错误：' + (e?.message || e));
    }
  }

  function updateInputEditorField(index, patch) {
    setInputEditorFields((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  }

  function saveInputEditor() {
    const cleaned = inputEditorFields.map((item) => {
      const next = { ...item };
      next.key = String(next.key || '').trim();
      next.label = String(next.label || '').trim() || next.key;
      next.type = next.type || 'text';
      next.required = !!next.required;
      next.visible_to_user = next.visible_to_user !== false;
      next.admin_fixed = !!next.admin_fixed;
      next.path_mode = next.path_mode === 'relative_to_module' ? 'relative_to_module' : 'absolute';
      return next;
    }).filter((item) => item.key);

    setModuleForm((prev) => ({ ...prev, inputs_text: JSON.stringify(cleaned, null, 2) }));
    setInputEditorOpen(false);
  }

  async function handleDeleteModule(moduleId) {
    if (!window.confirm(`确定删除模块 ${moduleId} 吗？`)) return;
    try {
      await deleteModuleApi(moduleId);
      await refreshModules();
    } catch (e) {
      alert(e?.message || '删除模块失败');
    }
  }

  async function handleAddUser() {
    try {
      await addUser(newUserForm);
      setNewUserForm({
        username: '',
        password: '',
        role: 'user',
        security_question: '',
        security_answer: '',
      });
      await refreshUsers();
    } catch (e) {
      alert(e?.message || '新增用户失败');
    }
  }
    async function loadUserFiles() {
      try {
        const files = await listUserFiles();
        setUserFiles(Array.isArray(files) ? files : []);
      } catch (e) {
        console.error('加载用户文件失败', e);
      }
    }

    async function handleUploadManagedFile(e) {
      const file = e.target.files?.[0];
      if (!file) return;

      try {
        setUploadingFile(true);
        await uploadUserFile(file);
        await loadUserFiles();
        alert('文件上传成功');
      } catch (err) {
        alert(err?.message || '文件上传失败');
      } finally {
        setUploadingFile(false);
        e.target.value = '';
      }
    }

  async function handleDeleteManagedFile(filename) {
    try {
      await deleteUserFile(filename);
      await loadUserFiles();
    } catch (err) {
      alert(err?.message || '删除失败');
    }
  }
  function buildFileTree(files) {
    const root = {
      name: 'uploads',
      type: 'dir',
      children: {},
      files: [],
    };

    (files || []).forEach((file) => {
      const rawPath = String(file.path || file.name || '');
      const normalized = rawPath.replace(/\\/g, '/');
      const parts = normalized.split('/').filter(Boolean);

      const fileName = file.name || parts[parts.length - 1] || '未命名文件';

      let usefulParts = parts;
      const uploadIndex = parts.findIndex((p) => p === 'uploads');
      if (uploadIndex >= 0) {
        usefulParts = parts.slice(uploadIndex + 1);
      }

      if (usefulParts.length <= 1) {
        root.files.push({ ...file, name: fileName });
        return;
      }

      const dirParts = usefulParts.slice(0, -1);
      let node = root;

      dirParts.forEach((part) => {
        if (!node.children[part]) {
          node.children[part] = {
            name: part,
            type: 'dir',
            children: {},
            files: [],
          };
        }
        node = node.children[part];
      });

      node.files.push({ ...file, name: fileName });
    });

    return root;
  }

  function renderFileTreeNode(node, depth = 0) {
    const childDirs = Object.values(node.children || {});
    const files = node.files || [];

    return (
      <div>
        {depth === 0 && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '7px 8px',
              borderRadius: 8,
              background: '#f3f7fc',
              color: '#1d3d63',
              fontWeight: 900,
              marginBottom: 6,
              minWidth: 0,
            }}
          >
            <span>▾</span>
            <span>📁</span>
            <span
              style={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {node.name}
            </span>
          </div>
        )}

        {childDirs.map((child) => (
          <div key={`${depth}-${child.name}`} style={{ marginLeft: depth === 0 ? 10 : 14 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '5px 6px',
                borderRadius: 8,
                color: '#26384d',
                fontSize: 13,
                minWidth: 0,
              }}
            >
              <span style={{ color: '#7b8ba1' }}>▾</span>
              <span>📁</span>
              <span
                title={child.name}
                style={{
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {child.name}
              </span>
            </div>

            {renderFileTreeNode(child, depth + 1)}
          </div>
        ))}

        {files.map((f) => (
          <div
            key={f.path || f.name}
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 1fr) auto',
              alignItems: 'center',
              gap: 6,
              marginLeft: depth === 0 ? 14 : 24,
              padding: '6px 6px',
              borderRadius: 8,
              color: '#26384d',
              minWidth: 0,
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = '#f5f9ff';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = 'transparent';
            }}
          >
            <div
              title={f.path}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                minWidth: 0,
                fontSize: 13,
                color: '#26384d',
              }}
            >
              <span style={{ color: '#7b8ba1' }}>▸</span>
              <span>📄</span>
              <span
                style={{
                  minWidth: 0,
                  overflow: 'hidden',
                  whiteSpace: 'nowrap',
                  textOverflow: 'ellipsis',
                }}
              >
                {f.name}
              </span>
            </div>

            {fileActionMode === 'copy' && (
              <button
                title="复制路径"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(f.path);
                    alert('路径已复制');
                  } catch {
                    alert('复制失败，请手动复制路径');
                  }
                }}
                style={{
                  border: 'none',
                  background: 'transparent',
                  color: '#2d7cf6',
                  fontSize: 15,
                  cursor: 'pointer',
                  padding: '0 4px',
                  lineHeight: 1,
                }}
              >
                📋
              </button>
            )}

            {fileActionMode === 'delete' && (
              <button
                title="删除文件"
                onClick={() => {
                  const ok = window.confirm(`确认删除文件：${f.name}？`);
                  if (ok) {
                    handleDeleteManagedFile(f.name);
                  }
                }}
                style={{
                  border: 'none',
                  background: 'transparent',
                  color: '#e53935',
                  fontSize: 18,
                  fontWeight: 900,
                  cursor: 'pointer',
                  padding: '0 4px',
                  lineHeight: 1,
                }}
              >
                ×
              </button>
            )}
          </div>
        ))}
      </div>
    );
  }
  async function handleDeleteUser(username) {
    try {
      await deleteUser(username);
      await refreshUsers();
    } catch (e) {
      alert(e?.message || '删除用户失败');
    }
  }

  async function handleRoleChange(username, role) {
    try {
      await updateUserRole(username, role);
      await refreshUsers();
    } catch (e) {
      alert(e?.message || '更新角色失败');
    }
  }

  async function handleEnabledChange(username, enabled) {
    try {
      await updateUserEnabled(username, enabled);
      await refreshUsers();
    } catch (e) {
      alert(e?.message || '更新状态失败');
    }
  }

  async function handleAdminResetPassword(username) {
    const newPassword = prompt(`请输入 ${username} 的新密码`);
    if (!newPassword) return;
    try {
      await adminResetPassword(username, newPassword);
      alert('密码已重置');
    } catch (e) {
      alert(e?.message || '重置密码失败');
    }
  }
  function renderFileManagerPanel() {
    const fileTree = buildFileTree(userFiles);

    return (
      <div
        style={{
          ...styles.card,
          padding: 14,
          minHeight: '100%',
          minWidth: 0,
          maxWidth: '100%',
          overflow: 'hidden',
          background: '#ffffff',
        }}
      >
        <div
          style={{
            fontSize: 22,
            fontWeight: 900,
            color: '#0b2d51',
            marginBottom: 12,
          }}
        >
          文件管理
        </div>

        <input
          ref={fileInputRef}
          type="file"
          style={{ display: 'none' }}
          onChange={handleUploadManagedFile}
        />

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 8,
            marginBottom: 12,
            padding: 8,
            border: '1px solid #d8e3ef',
            borderRadius: 12,
            background: '#ffffff',
          }}
        >
          <button
            style={{
              ...styles.blueBtn,
              height: 36,
              borderRadius: 8,
              padding: '0 8px',
              fontSize: 13,
            }}
            onClick={() => fileInputRef.current?.click()}
            disabled={uploadingFile}
          >
            {uploadingFile ? '上传中...' : '上传文件'}
          </button>

          <button
            style={{
              ...styles.whiteBtn,
              height: 36,
              borderRadius: 8,
              padding: '0 8px',
              fontSize: 13,
            }}
            onClick={loadUserFiles}
          >
            刷新列表
          </button>

          <button
            style={{
              ...(fileActionMode === 'copy' ? styles.blueBtn : styles.whiteBtn),
              height: 36,
              borderRadius: 8,
              padding: '0 8px',
              fontSize: 13,
            }}
            onClick={() =>
              setFileActionMode((prev) => (prev === 'copy' ? '' : 'copy'))
            }
          >
            复制路径
          </button>

          <button
            style={{
              ...(fileActionMode === 'delete' ? styles.redBtn : styles.whiteBtn),
              height: 36,
              borderRadius: 8,
              padding: '0 8px',
              fontSize: 13,
            }}
            onClick={() =>
              setFileActionMode((prev) => (prev === 'delete' ? '' : 'delete'))
            }
          >
            删除文件
          </button>
        </div>

        <div
          style={{
            fontSize: 14,
            fontWeight: 800,
            color: '#4b6078',
            marginBottom: 8,
          }}
        >
          已上传文件
        </div>

        <div
          style={{
            border: '1px solid #d8e3ef',
            borderRadius: 12,
            background: '#ffffff',
            minHeight: 320,
            maxHeight: 'calc(100vh - 270px)',
            overflowY: 'auto',
            overflowX: 'hidden',
            padding: 8,
            minWidth: 0,
          }}
        >
          {userFiles.length === 0 ? (
            <div
              style={{
                color: '#99a6b5',
                fontSize: 14,
                padding: '12px 10px',
              }}
            >
              暂无文件
            </div>
          ) : (
            renderFileTreeNode(fileTree)
          )}
        </div>
      </div>
    );
  }
  function renderModuleRuntime(module) {
    if (!module) {
      return <div style={{ padding: 20 }}>当前没有匹配到可运行模块</div>;
    }

    const form = runtimeForms[module.id] || { task_name: module.name };

    return (
      <>
        <div style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 30, fontWeight: 900, color: '#0b2d51' }}>{module.name}</div>
          <div style={{ color: '#617892', marginTop: 6 }}>参数选择与本路径配置</div>
        </div>

        <div style={{ display: 'grid', gap: 18, maxWidth: 980 }}>
          <label>
            <div style={{ fontWeight: 800, color: '#173353', marginBottom: 8 }}>任务名称</div>
            <input
              value={form.task_name || ''}
              onChange={(e) =>
                setRuntimeForms((prev) => ({
                  ...prev,
                  [module.id]: {
                    ...prev[module.id],
                    task_name: e.target.value,
                  },
                }))
              }
              style={styles.input}
            />
          </label>

          <label>
            <div style={{ fontWeight: 800, color: '#173353', marginBottom: 8 }}>
              并行进程数
            </div>
            <select
              value={form._parallel_workers || 1}
              onChange={(e) =>
                setRuntimeForms((prev) => ({
                  ...prev,
                  [module.id]: {
                    ...prev[module.id],
                    _parallel_workers: Number(e.target.value),
                  },
                }))
              }
              style={styles.input}
            >
              {[1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 24, 32].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
            <div style={{ marginTop: 6, color: '#6a7f96', fontSize: 13, lineHeight: 1.6 }}>
              单文件模块：输入字段可填文件夹，平台按文件拆成多个进程；文件夹遍历模块：平台把输入文件夹拆成多个临时子文件夹并行运行。
            </div>
          </label>

          {(module.inputs || []).filter(isFieldVisibleToUser).map((field) => (
            <label key={field.key}>
              <div style={{ fontWeight: 800, color: '#173353', marginBottom: 8 }}>
                {field.label || field.key}
              </div>
              {field.type === 'file_path' || field.type === 'dir_path' ? (
                <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <input
                    value={form[field.key] || ''}
                    onChange={(e) =>
                      setRuntimeForms((prev) => ({
                        ...prev,
                        [module.id]: {
                          ...prev[module.id],
                          [field.key]: e.target.value,
                        },
                      }))
                    }
                    style={{ ...styles.input, flex: 1 }}
                  />
                  <button style={styles.whiteBtn} onClick={() => browseField(module, field)}>
                    浏览
                  </button>
                </div>
              ) : (
                <input
                  value={form[field.key] || ''}
                  onChange={(e) =>
                    setRuntimeForms((prev) => ({
                      ...prev,
                      [module.id]: {
                        ...prev[module.id],
                        [field.key]: e.target.value,
                      },
                    }))
                  }
                  style={styles.input}
                />
              )}
            </label>
          ))}
        </div>

        <div style={{ marginTop: 22 }}>
          <button style={{ ...styles.blueBtn, padding: '12px 28px' }} onClick={() => runGeneric(module)}>
            运行
          </button>
        </div>
      </>
    );
  }

  function renderToolbarOptions() {
    return visibleToolbars.map((tb) => (
      <option key={tb.key} value={tb.key}>
        {tb.label}
      </option>
    ));
  }

  function renderToolbarAdminList() {
    return (
      <div
        style={{
          border: '1px solid #d7e3f0',
          borderRadius: 12,
          background: '#fff',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1.3fr 1fr 70px 128px',
            gap: 8,
            padding: '10px 12px',
            background: 'rgba(240,246,252,0.95)',
            color: '#1a3c63',
            fontWeight: 900,
            fontSize: 13,
          }}
        >
          <div>名称</div>
          <div>标识</div>
          <div>模块</div>
          <div>操作</div>
        </div>

        {visibleToolbars.map((tb) => {
          const list = modulesByTool[tb.key] || [];
          const isEditing = editingToolbarKey === tb.key;

          return (
            <div
              key={tb.key}
              style={{
                display: 'grid',
                gridTemplateColumns: '1.3fr 1fr 70px 128px',
                gap: 8,
                alignItems: 'center',
                padding: '10px 12px',
                borderTop: '1px solid #edf2f7',
                fontSize: 13,
              }}
            >
              {isEditing ? (
                <>
                  <input
                    placeholder="工具类型名称"
                    value={toolbarEditForm.label}
                    onChange={(e) => setToolbarEditForm({ ...toolbarEditForm, label: e.target.value })}
                    style={{ ...styles.input, minHeight: 36, fontSize: 13 }}
                  />
                  <input
                    placeholder="工具类型标识"
                    value={toolbarEditForm.key}
                    onChange={(e) => setToolbarEditForm({ ...toolbarEditForm, key: e.target.value })}
                    style={{ ...styles.input, minHeight: 36, fontSize: 13 }}
                  />
                  <div style={{ color: '#6a7f96' }}>{list.length}</div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button style={{ ...styles.blueBtn, padding: '8px 10px', fontSize: 13 }} onClick={handleUpdateToolbar}>保存</button>
                    <button style={{ ...styles.whiteBtn, padding: '8px 10px', fontSize: 13 }} onClick={cancelEditToolbar}>取消</button>
                  </div>
                </>
              ) : (
                <>
                  <div style={{ fontWeight: 800, color: '#12385f' }}>
                    {tb.label}
                  </div>
                  <div style={{ color: '#6a7f96', wordBreak: 'break-all' }}>{tb.key}</div>
                  <div style={{ color: '#6a7f96' }}>{list.length}</div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button style={{ ...styles.whiteBtn, padding: '8px 10px', fontSize: 13 }} onClick={() => startEditToolbar(tb)}>编辑</button>
                    <button
                      style={{ ...styles.redBtn, padding: '8px 10px', fontSize: 13 }}
                      title={list.length > 0 ? '删除工具栏后模块会自动移动到其它工具栏' : ''}
                      onClick={() => handleDeleteToolbar(tb)}
                    >
                      删除
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  function renderInstalledModulesTree() {
    return (
      <div style={{ display: 'grid', gap: 10, marginTop: 12, maxHeight: 'calc(100vh - 520px)', overflow: 'auto' }}>
        {visibleToolbars.map((tb) => {
          const list = modulesByTool[tb.key] || [];
          const expanded = expandedToolTypes[tb.key] !== false;
          return (
            <div key={tb.key} style={{ border: '1px solid #d6e2ef', background: '#fff', borderRadius: 12, overflow: 'hidden' }}>
              <button
                style={{ ...styles.whiteBtn, width: '100%', border: 'none', borderRadius: 0, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                onClick={() => setExpandedToolTypes((prev) => ({ ...prev, [tb.key]: !expanded }))}
              >
                <span>{expanded ? '▼' : '▶'} {tb.label}</span>
                <span style={{ color: '#6a7f96' }}>{list.length} 个模块</span>
              </button>

              {expanded && (
                <div style={{ padding: 10, display: 'grid', gap: 10 }}>
                  {list.length === 0 && <div style={{ color: '#9aa8b8', fontSize: 13 }}>暂无模块</div>}
                  {list.map((m) => (
                    <div key={m.id} style={{ border: '1px solid #e2ebf5', background: '#fbfdff', borderRadius: 10, padding: 10 }}>
                      <div style={{ fontWeight: 800, color: '#12385f' }}>{m.name}</div>
                      <div style={{ color: '#6a7f96', marginTop: 4, wordBreak: 'break-all' }}>{m.id}</div>
                      {m.enabled === false && <div style={{ color: '#b45309', marginTop: 4 }}>已禁用</div>}
                      <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                        <button style={styles.whiteBtn} onClick={() => fillModuleForm(m)}>编辑</button>
                        <button style={styles.redBtn} onClick={() => handleDeleteModule(m.id)}>删除</button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  function renderToolPage(toolKey) {
    const toolbar = visibleToolbars.find((t) => t.key === toolKey) || { key: toolKey, label: toolKey };
    const list = modulesByTool[toolKey] || [];
    const selectedId = activeModuleByTool[toolKey] || list[0]?.id || '';
    const selectedModule = list.find((m) => m.id === selectedId) || list[0] || null;

    return (
      <section
        style={{
          display: 'grid',
          gridTemplateColumns: '280px minmax(0, 1fr) 320px',
          gap: 12,
          minHeight: 'calc(100vh - 98px)',
          width: '100%',
          overflow: 'hidden',
        }}
      >
        <div style={{ ...styles.card, padding: 18 }}>
          <div style={{ fontSize: 22, fontWeight: 900, color: '#0b2d51', marginBottom: 16 }}>
            {toolbar.label}模块
          </div>

          <div style={{ display: 'grid', gap: 12 }}>
            {list.length === 0 && (
              <div style={{ color: '#8998a8', lineHeight: 1.8 }}>
                这个工具栏下还没有模块。管理员可以在“模块管理”中选择该工具类型后安装或手工添加模块。
              </div>
            )}
            {list.map((m) => (
              <button
                key={m.id}
                onClick={() => setActiveModuleByTool((prev) => ({ ...prev, [toolKey]: m.id }))}
                style={{
                  textAlign: 'left',
                  padding: '18px 16px',
                  borderRadius: 14,
                  border: selectedModule?.id === m.id ? '2px solid #2b73db' : '1px solid #d7e3f0',
                  background:
                    selectedModule?.id === m.id
                      ? 'linear-gradient(135deg, rgba(41,118,210,0.13), rgba(89,176,255,0.08))'
                      : '#fff',
                  cursor: 'pointer',
                }}
              >
                <div style={{ fontWeight: 800, fontSize: 20, color: '#13385f' }}>{m.name}</div>
                <div style={{ marginTop: 8, color: '#60748b', lineHeight: 1.7 }}>{m.description || m.id}</div>
              </button>
            ))}
          </div>
        </div>

        <div style={{ ...styles.card, padding: 22 }}>
          {selectedModule ? renderModuleRuntime(selectedModule) : <div style={{ padding: 20, color: '#999' }}>当前工具栏暂无可运行模块</div>}
        </div>

        {renderFileManagerPanel()}
      </section>
    );
  }

  if (!currentUser) {
    return (
      <>
        {startupError && (
          <div
            style={{
              position: 'fixed',
              top: 16,
              right: 16,
              zIndex: 9999,
              background: '#fff4e5',
              color: '#8a4b08',
              border: '1px solid #f3d3a4',
              padding: '10px 14px',
              borderRadius: 10,
              fontSize: 14,
            }}
          >
            {startupError}
          </div>
        )}
        <LoginPage
          authMode={authMode}
          setAuthMode={setAuthMode}
          loginType={loginType}
          setLoginType={setLoginType}
          loginForm={loginForm}
          setLoginForm={setLoginForm}
          registerForm={registerForm}
          setRegisterForm={setRegisterForm}
          forgotForm={forgotForm}
          setForgotForm={setForgotForm}
          loginError={loginError}
          handleLogin={handleLogin}
          handleRegister={handleRegister}
          handleForgotQuestion={handleForgotQuestion}
          handleForgotReset={handleForgotReset}
        />
      </>
    );
  }

  return (
    <div style={styles.page}>
      <div style={styles.topbar}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
          <div style={{ fontSize: 26, fontWeight: 900 }}>云和气溶胶反演系统</div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {navItems.map((item) => (
                <button
                    key={item.key}
                    onClick={() => {
                      setActiveTab(item.key);
                      saveActiveTab(item.key);
                    }}
                    style={activeTab === item.key ? styles.topBtnActive : styles.topBtn}
                >
                  {item.label}
                </button>
            ))}
          </div>
        </div>

        <div style={{display: 'flex', gap: 12, alignItems: 'center'}}>
          <div style={{fontWeight: 700}}>
            当前用户：{currentUser.username}（{currentUser.role}）
          </div>
          <button style={styles.topBtn} onClick={handleLogout}>退出登录</button>
        </div>
      </div>

      <div style={{padding: 12, overflowX: 'hidden'}}>
        {activeTab === 'module_mgmt' && isAdmin && (
            <section style={{...styles.card, padding: 16, minHeight: 'calc(100vh - 98px)'}}>
              <div style={{display: 'grid', gridTemplateColumns: '380px 1fr', gap: 16}}>
                <div style={{...styles.card, padding: 16}}>
                  <div style={{fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 12}}>
                    模块上传 / 本地投放
                  </div>

                  <div style={{display: 'grid', gap: 10, marginBottom: 14}}>
                    <label>
                      <div style={labelStyle}>模块所属工具栏</div>
                      <select value={uploadToolType} onChange={(e) => setUploadToolType(e.target.value)}
                              style={styles.input}>
                        {renderToolbarOptions()}
                      </select>
                    </label>

                    <label>
                      <div style={labelStyle}>可选：上传模块 zip</div>
                      <input type="file" accept=".zip" onChange={(e) => setZipFile(e.target.files?.[0] || null)}/>
                    </label>
                  </div>

                  <div style={{display: 'flex', gap: 10, flexWrap: 'wrap'}}>
                    <button style={styles.blueBtn} onClick={uploadZip}>上传并安装</button>
                    <button style={styles.blueBtn} onClick={() => installFromDrop()}>扫描本地目录安装</button>
                    <button style={styles.whiteBtn} onClick={refreshDropZipList}>刷新目录</button>
                    <button style={styles.whiteBtn} onClick={() => setShowDropHint(true)}>本地模块目录说明</button>
                  </div>

                  {uploadMsg && <div style={{marginTop: 12, color: '#4f6682'}}>{uploadMsg}</div>}
                  {dropInfo.drop_dir && (
                      <div style={{marginTop: 12, color: '#6a7f96', fontSize: 13, wordBreak: 'break-all'}}>
                        本地投放目录：{dropInfo.drop_dir}
                      </div>
                  )}

                  {dropInfo.items?.length > 0 && (
                      <div style={{marginTop: 12, display: 'grid', gap: 8}}>
                        <div style={{fontWeight: 800, color: '#12385f'}}>目录中待安装 zip</div>
                        {dropInfo.items.map((item) => (
                            <div key={item.path} style={{
                              border: '1px solid #e1eaf3',
                              background: '#fff',
                              borderRadius: 10,
                              padding: 10
                            }}>
                              <div style={{fontWeight: 700, wordBreak: 'break-all'}}>{item.name}</div>
                              <button style={{...styles.whiteBtn, marginTop: 8}}
                                      onClick={() => installFromDrop(item.name)}>
                                安装这个 zip
                              </button>
                            </div>
                        ))}
                      </div>
                  )}

                  <div style={{marginTop: 20, fontSize: 20, fontWeight: 900, color: '#12385f'}}>
                    已安装模块
                  </div>
                  {renderInstalledModulesTree()}

                  <div style={{marginTop: 20, fontSize: 18, fontWeight: 900, color: '#12385f', marginBottom: 10}}>
                    工具栏列表
                  </div>
                  <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 8, marginBottom: 10}}>
                    <input
                        placeholder="新增工具栏名称"
                        value={newToolbarForm.label}
                        onChange={(e) => setNewToolbarForm({...newToolbarForm, label: e.target.value})}
                        style={{...styles.input, minHeight: 38, fontSize: 13}}
                    />
                    <input
                        placeholder="标识，可选"
                        value={newToolbarForm.key}
                        onChange={(e) => setNewToolbarForm({...newToolbarForm, key: e.target.value})}
                        style={{...styles.input, minHeight: 38, fontSize: 13}}
                    />
                    <button style={{...styles.blueBtn, padding: '8px 12px', fontSize: 13}}
                            onClick={handleAddToolbar}>添加
                    </button>
                  </div>
                  {renderToolbarAdminList()}
                </div>

                <div style={{...styles.card, padding: 16}}>
                  <div style={{fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 12}}>
                    {editingModuleId ? `编辑模块：${editingModuleId}` : '手工新增 / 更新模块'}
                  </div>

                  <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12}}>
                    <select value={moduleForm.tool_type}
                            onChange={(e) => setModuleForm({...moduleForm, tool_type: e.target.value})}
                            style={styles.input}>
                      {renderToolbarOptions()}
                    </select>
                    <input placeholder="ID" value={moduleForm.id}
                           onChange={(e) => setModuleForm({...moduleForm, id: e.target.value})} style={styles.input}/>
                    <input placeholder="名称" value={moduleForm.name}
                           onChange={(e) => setModuleForm({...moduleForm, name: e.target.value})} style={styles.input}/>
                    <input placeholder="可执行文件" value={moduleForm.executable}
                           onChange={(e) => setModuleForm({...moduleForm, executable: e.target.value})}
                           style={styles.input}/>
                    <input placeholder="工作目录" value={moduleForm.working_dir}
                           onChange={(e) => setModuleForm({...moduleForm, working_dir: e.target.value})}
                           style={styles.input}/>
                    <input placeholder="标签，英文逗号分隔" value={moduleForm.tags_text}
                           onChange={(e) => setModuleForm({...moduleForm, tags_text: e.target.value})}
                           style={styles.input}/>

                    <textarea placeholder="描述" value={moduleForm.description}
                              onChange={(e) => setModuleForm({...moduleForm, description: e.target.value})}
                              style={{...styles.textarea, gridColumn: '1 / span 2', minHeight: 80}}/>
                    <textarea placeholder="命令模板(JSON数组)" value={moduleForm.command_template_text}
                              onChange={(e) => setModuleForm({...moduleForm, command_template_text: e.target.value})}
                              style={{...styles.textarea, gridColumn: '1 / span 2'}}/>
                    <textarea placeholder="输入字段(JSON数组)：包含输入/输出路径、是否用户可见、管理员预填 resources 等"
                              value={moduleForm.inputs_text}
                              onChange={(e) => setModuleForm({...moduleForm, inputs_text: e.target.value})}
                              style={{...styles.textarea, gridColumn: '1 / span 2', minHeight: 180}}/>
                    <textarea placeholder="并行配置(JSON对象)，保存在 module.json 的 parallel 字段"
                              value={moduleForm.parallel_json_text}
                              onChange={(e) => setModuleForm({...moduleForm, parallel_json_text: e.target.value})}
                              style={{...styles.textarea, gridColumn: '1 / span 2', minHeight: 110}}/>
                  </div>

                  <div style={{display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap'}}>
                    <button style={styles.blueBtn} onClick={saveCurrentModule}>保存模块</button>
                    <button style={styles.whiteBtn} onClick={openInputEditor}>编辑输入文件</button>
                    <button
                        style={styles.whiteBtn}
                        onClick={() => {
                          setEditingModuleId('');
                          setModuleForm(emptyModuleForm);
                        }}
                    >
                      新建空白
                    </button>
                  </div>
                </div>
              </div>
            </section>
        )}
        {activeTab === 'user_mgmt' && isAdmin && (
            <section style={{...styles.card, padding: 16, minHeight: 'calc(100vh - 98px)'}}>
              <div style={{fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 14}}>
                用户管理
              </div>

              <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr 180px', gap: 12}}>
                <input placeholder="用户名" value={newUserForm.username}
                       onChange={(e) => setNewUserForm({...newUserForm, username: e.target.value})}
                       style={styles.input}/>
                <input placeholder="密码" type="password" value={newUserForm.password}
                       onChange={(e) => setNewUserForm({...newUserForm, password: e.target.value})}
                       style={styles.input}/>
                <select value={newUserForm.role}
                        onChange={(e) => setNewUserForm({...newUserForm, role: e.target.value})} style={styles.input}>
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
                <input placeholder="安全问题" value={newUserForm.security_question}
                       onChange={(e) => setNewUserForm({...newUserForm, security_question: e.target.value})}
                       style={{...styles.input, gridColumn: '1 / span 2'}}/>
                <input placeholder="安全答案" value={newUserForm.security_answer}
                       onChange={(e) => setNewUserForm({...newUserForm, security_answer: e.target.value})}
                       style={styles.input}/>
              </div>

              <div style={{marginTop: 12}}>
                <button style={styles.blueBtn} onClick={handleAddUser}>新增用户</button>
              </div>

              <div style={{overflowX: 'auto', marginTop: 16}}>
                <table style={{width: '100%', borderCollapse: 'collapse', background: '#fff'}}>
                  <thead>
                  <tr>
                    <th style={thStyle}>用户名</th>
                    <th style={thStyle}>角色</th>
                    <th style={thStyle}>状态</th>
                    <th style={thStyle}>操作</th>
                  </tr>
                  </thead>
                  <tbody>
                  {users.map((u) => (
                      <tr key={u.username}>
                        <td style={tdStyle}>{u.username}</td>
                        <td style={tdStyle}>
                          <select value={u.role} onChange={(e) => handleRoleChange(u.username, e.target.value)}
                                  style={styles.input}>
                            <option value="user">user</option>
                            <option value="admin">admin</option>
                          </select>
                        </td>
                        <td style={tdStyle}>
                          <select value={u.enabled ? 'enabled' : 'disabled'}
                                  onChange={(e) => handleEnabledChange(u.username, e.target.value === 'enabled')}
                                  style={styles.input}>
                            <option value="enabled">enabled</option>
                            <option value="disabled">disabled</option>
                          </select>
                        </td>
                        <td style={tdStyle}>
                          <div style={{display: 'flex', gap: 8}}>
                            <button style={styles.whiteBtn}
                                    onClick={() => handleAdminResetPassword(u.username)}>重置密码
                            </button>
                            {u.username !== 'admin' && (
                                <button style={styles.redBtn} onClick={() => handleDeleteUser(u.username)}>删除</button>
                            )}
                          </div>
                        </td>
                      </tr>
                  ))}
                  </tbody>
                </table>
              </div>
            </section>
        )}
        {activeTab.startsWith('tool:') && renderToolPage(activeTab.slice('tool:'.length))}
        {activeTab === 'tasks' && (
            <section style={{...styles.card, padding: 18, minHeight: 'calc(100vh - 98px)'}}>
              <div style={{fontSize: 28, fontWeight: 900, color: '#0b2d51', marginBottom: 16}}>任务列表</div>
              <div style={{overflowX: 'auto'}}>
                <table style={{width: '100%', borderCollapse: 'collapse', background: '#fff'}}>
                  <thead>
                  <tr>
                    <th style={thStyle}>任务 ID</th>
                    <th style={thStyle}>模块</th>
                    <th style={thStyle}>类型</th>
                    <th style={thStyle}>状态</th>
                    <th style={thStyle}>开始时间</th>
                    <th style={thStyle}>结束时间</th>
                    <th style={thStyle}>操作</th>
                  </tr>
                  </thead>
                  <tbody>
                  {tasks.map((task) => (
                      <tr key={task.id}>
                        <td style={tdStyle}>{task.id}</td>
                        <td style={tdStyle}>{task.module_name}</td>
                        <td style={tdStyle}>{task.kind}</td>
                        <td style={tdStyle}>{statusBadge(task.status)}</td>
                        <td style={tdStyle}>{task.started_at || '-'}</td>
                        <td style={tdStyle}>{task.ended_at || '-'}</td>
                        <td style={tdStyle}>
                          <div style={{display: 'flex', gap: 8, flexWrap: 'wrap'}}>
                            <button
                                style={styles.whiteBtn}
                                onClick={async () => {
                                  try {
                                    const detail = await getTask(task.id);
                                    addTaskWindow(detail, task.module_name || task.id);
                                  } catch (e) {
                                    alert(e?.message || '获取任务详情失败');
                                  }
                                }}
                            >
                              查看
                            </button>

                            {(task.status === 'running' || task.status === 'queued') && (
                                <button
                                    style={styles.redBtn}
                                    onClick={async () => {
                                      try {
                                        await cancelTask(task.id);
                                        await refreshTasks();
                                      } catch (e) {
                                        alert(e?.message || '关闭失败');
                                      }
                                    }}
                                >
                                  关闭
                                </button>
                            )}

                            <button style={styles.redBtn} onClick={() => handleDeleteTask(task.id)}>
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                  ))}
                  {tasks.length === 0 && (
                      <tr>
                        <td colSpan={7} style={{padding: 30, textAlign: 'center', color: '#6c8098'}}>
                          暂无任务
                        </td>
                      </tr>
                  )}
                  </tbody>
                </table>
              </div>
            </section>
        )}
      </div>

      {windows.filter((w) => !w.minimized).map((w) => (
        <TaskWindow
          key={w.id}
          win={w}
          onMin={(id) =>
            setWindows((prev) => prev.map((x) => (x.id === id ? { ...x, minimized: true } : x)))
          }
          onClose={(id) => setWindows((prev) => prev.filter((x) => x.id !== id))}
          onFront={bringFront}
          onMove={moveWindow}
          onStop={stopTaskWindow}
        />
      ))}

      {windows.length > 0 && (
        <div
          style={{
            position: 'fixed',
            right: 12,
            bottom: 12,
            width: 280,
            ...styles.card,
            padding: 14,
            zIndex: 5000,
          }}
        >
          <div style={{ fontWeight: 900, fontSize: 18, color: '#123a64', marginBottom: 10 }}>任务托盘</div>
          <div style={{ display: 'grid', gap: 8 }}>
            {windows.filter((w) => w.minimized).length === 0 && (
              <div style={{ color: '#6b8097' }}>当前无最小化任务</div>
            )}
            {windows.filter((w) => w.minimized).map((w) => (
              <button
                key={w.id}
                onClick={() =>
                  setWindows((prev) =>
                    prev.map((x) => (x.id === w.id ? { ...x, minimized: false, zIndex: ++zRef.current } : x))
                  )
                }
                style={{
                  border: '1px solid #d6e2ef',
                  background: '#fff',
                  borderRadius: 12,
                  padding: '10px 12px',
                  textAlign: 'left',
                  cursor: 'pointer',
                }}
              >
                <div style={{ fontWeight: 800, color: '#12385f' }}>{w.title}</div>
                <div style={{ color: '#6a7f96', marginTop: 4 }}>{w.task?.status || '-'}</div>
              </button>
            ))}
          </div>
        </div>
      )}


      {inputEditorOpen && (
        <SimpleOverlay
          title="编辑输入文件"
          onClose={() => setInputEditorOpen(false)}
          width="min(1180px, 96vw)"
        >
          <div style={{ color: '#173353', lineHeight: 1.7 }}>
            <div style={{ marginBottom: 12, color: '#5f7088' }}>
              这里设置每个输入字段是否需要用户填写。选择“管理员预填/隐藏”后，用户运行界面不会显示该字段；默认值可以写 resources 里的相对路径，例如 resources/ConfigXMLFile.xml。
            </div>

            <div style={{ display: 'grid', gap: 12 }}>
              {inputEditorFields.map((field, index) => (
                <div
                  key={index}
                  style={{
                    border: '1px solid #d7e3f0',
                    background: '#fff',
                    borderRadius: 12,
                    padding: 12,
                  }}
                >
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 130px 110px', gap: 10 }}>
                    <input
                      placeholder="key，例如 input_file"
                      value={field.key || ''}
                      onChange={(e) => updateInputEditorField(index, { key: e.target.value })}
                      style={styles.input}
                    />
                    <input
                      placeholder="显示名称"
                      value={field.label || ''}
                      onChange={(e) => updateInputEditorField(index, { label: e.target.value })}
                      style={styles.input}
                    />
                    <select
                      value={field.type || 'text'}
                      onChange={(e) => updateInputEditorField(index, { type: e.target.value })}
                      style={styles.input}
                    >
                      <option value="text">text</option>
                      <option value="textarea">textarea</option>
                      <option value="number">number</option>
                      <option value="file_path">file_path</option>
                      <option value="dir_path">dir_path</option>
                      <option value="password">password</option>
                    </select>
                    <select
                      value={field.required ? 'true' : 'false'}
                      onChange={(e) => updateInputEditorField(index, { required: e.target.value === 'true' })}
                      style={styles.input}
                    >
                      <option value="true">必填</option>
                      <option value="false">非必填</option>
                    </select>
                  </div>

                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px 170px', gap: 10, marginTop: 10 }}>
                    <input
                      placeholder="默认值 / 管理员预填路径，例如 resources/ConfigXMLFile.xml"
                      value={field.default ?? ''}
                      onChange={(e) => updateInputEditorField(index, { default: e.target.value })}
                      style={styles.input}
                    />
                    <select
                      value={field.visible_to_user === false ? 'hidden' : 'visible'}
                      onChange={(e) => {
                        const visible = e.target.value === 'visible';
                        updateInputEditorField(index, { visible_to_user: visible, admin_fixed: !visible });
                      }}
                      style={styles.input}
                    >
                      <option value="visible">用户输入</option>
                      <option value="hidden">用户隐藏</option>
                    </select>
                    <select
                      value={field.path_mode || 'absolute'}
                      onChange={(e) => updateInputEditorField(index, { path_mode: e.target.value })}
                      style={styles.input}
                    >
                      <option value="absolute">绝对路径/原样</option>
                      <option value="relative_to_module">相对模块目录</option>
                    </select>
                  </div>

                  <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 10, marginTop: 10 }}>
                    <input
                      placeholder="说明 help_text"
                      value={field.help_text || ''}
                      onChange={(e) => updateInputEditorField(index, { help_text: e.target.value })}
                      style={styles.input}
                    />
                    <button
                      style={styles.redBtn}
                      onClick={() => setInputEditorFields((prev) => prev.filter((_, i) => i !== index))}
                    >
                      删除
                    </button>
                  </div>

                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, flexDirection: 'row' }}>
                    <input
                      type="checkbox"
                      checked={!!field.admin_fixed}
                      onChange={(e) => updateInputEditorField(index, { admin_fixed: e.target.checked, visible_to_user: e.target.checked ? false : field.visible_to_user !== false })}
                      style={{ width: 'auto' }}
                    />
                    <span>管理员预填/隐藏：适合 resources 文件夹里的固定 XML、LUT、模型、掩膜等资源</span>
                  </label>
                </div>
              ))}
            </div>

            <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
              <button style={styles.whiteBtn} onClick={() => setInputEditorFields((prev) => [...prev, makeEmptyInputField()])}>新增输入字段</button>
              <button style={styles.blueBtn} onClick={saveInputEditor}>保存输入配置</button>
              <button style={styles.whiteBtn} onClick={() => setInputEditorOpen(false)}>取消</button>
            </div>
          </div>
        </SimpleOverlay>
      )}

      {showDropHint && (
        <SimpleOverlay
          title="本地模块目录投放说明"
          onClose={() => setShowDropHint(false)}
          width="min(820px, 96vw)"
        >
          <div style={{ lineHeight: 1.9, color: '#173353' }}>
            <p>你这个系统是本地服务器 + 本地浏览器模式，可以支持“把压缩包放到指定目录就相当于添加模块”的方式。</p>
            <p>
              当前后端会自动创建并扫描本地投放目录：
              <code>{dropInfo.drop_dir || '项目根目录/module_drop'}</code>
            </p>
            <ol>
              <li>管理员先在“模块所属工具栏”里选择云反演、气溶胶反演或自定义工具类型</li>
              <li>把模块 zip 直接放进这个目录，不需要在网页里选择文件</li>
              <li>点击“扫描本地目录安装”，后端会安装 zip，并把安装成功的 zip 移到 module_drop/installed</li>
            </ol>
          </div>
        </SimpleOverlay>
      )}
    </div>
  );
}

const thStyle = {
  textAlign: 'left',
  padding: '14px 12px',
  color: '#1a3c63',
  fontWeight: 800,
  borderBottom: '1px solid #e1eaf3',
  background: 'rgba(240,246,252,0.95)',
};

const tdStyle = {
  padding: '12px',
  borderBottom: '1px solid #edf2f7',
  color: '#203a58',
};