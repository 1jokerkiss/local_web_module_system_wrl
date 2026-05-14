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
  getSystemResources,
  runModule,
  saveModule,
  deleteModule as deleteModuleApi,
    uploadModuleFolder,
  validateCppModuleFolder,
  uploadPythonFolderModule,
  parseModuleParamJson,
  parsePythonModuleConfig,
  uploadPythonModuleConfig,
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
    listDataFiles,
  previewDataFile,
  revealDataFile,
  deleteDataFile,
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


const cppExecutableModuleTemplate = {
  id: 'parasol_aod',
  name: 'PARASOL AOD 反演',
  description: 'C++ 可执行模块示例：module.json、exe、resources、deps 同级放置。C++ 模块不需要上传源码。',
  runtime: 'cpp_native',
  executable: 'ParasolAOD.exe',
  working_dir: '.',
  config_mode: 'none',
  dependency_dirs: ['deps'],
  dependency_search_dirs: [],
  auto_collect_deps: true,
  command_template: ['{executable}', '{input_file}', '{output_dir}', '{config_xml}'],
  parallel: {
    mode: 'auto',
    file_patterns: '*.*',
    output_suffix: '.tif',
    output_naming: 'source_stem',
  },
  tags: ['cpp', 'native', 'remote-sensing'],
  enabled: true,
  inputs: [
    {
      key: 'input_file',
      label: '输入文件目录',
      type: 'dir_path',
      required: true,
      visible_to_user: true,
      admin_fixed: false,
      path_mode: 'absolute',
      batch_role: 'input',
      match_mode: 'each_file',
      io_role: 'input',
    },
    {
      key: 'output_dir',
      label: '输出目录',
      type: 'dir_path',
      required: true,
      visible_to_user: true,
      admin_fixed: false,
      path_mode: 'absolute',
      io_role: 'output',
    },
    {
      key: 'config_xml',
      label: '配置 XML',
      type: 'file_path',
      required: true,
      default: 'resources/ConfigXMLFile.xml',
      visible_to_user: false,
      admin_fixed: true,
      path_mode: 'relative_to_module',
      io_role: 'input',
    },
  ],
};

function getCppExecutableModuleTemplateText() {
  return JSON.stringify(cppExecutableModuleTemplate, null, 2);
}

function downloadTextFile(filename, text) {
  const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

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

function isParallelWorkerField(field) {
  const key = normalize(field?.key);
  const label = String(field?.label || '');
  const text = `${key} ${label}`;
  return (
    key === 'parallel_workers' ||
    key === '_parallel_workers' ||
    key === 'workers' ||
    key === 'worker_count' ||
    key === 'process_count' ||
    key === 'processes' ||
    key === 'num_processes' ||
    key === 'n_processes' ||
    key === 'nproc' ||
    (text.includes('进程数') && (text.includes('并行') || text.includes('并发'))) ||
    text.includes('parallel worker') ||
    text.includes('parallel_workers')
  );
}

function clampParallelWorkersValue(value, maxWorkers = 64) {
  const max = Math.max(1, Number.parseInt(String(maxWorkers || 64), 10) || 64);
  const n = Number.parseInt(String(value ?? '1').trim(), 10);
  if (!Number.isFinite(n)) return 1;
  return Math.max(1, Math.min(n, max));
}

function getConservativeSuggestedWorkers(cpuCount) {
  const cpu = Math.max(1, Number.parseInt(String(cpuCount || 1), 10) || 1);
  return Math.max(1, Math.min(8, Math.ceil(cpu / 3)));
}

function getConservativeMaxWorkers(cpuCount, suggestedWorkers) {
  const cpu = Math.max(1, Number.parseInt(String(cpuCount || 1), 10) || 1);
  const suggested = Math.max(1, Number.parseInt(String(suggestedWorkers || getConservativeSuggestedWorkers(cpu)), 10) || 1);
  return Math.max(suggested, Math.min(12, Math.max(1, Math.floor(cpu / 2))));
}

const defaultSystemResources = {
  cpu_count: 1,
  suggested_workers: 1,
  max_workers: 1,
  running_workers: 0,
  available_workers: 1,
  active_task_count: 0,
  queued_task_count: 0,
  cpu_percent: null,
  running_process_cpu_percent: null,
  cpu_busy_threshold: 85,
  active_tasks: [],
};

function normalizeSystemResources(data) {
  const cpuCount = Math.max(1, Number.parseInt(String(data?.cpu_count || 1), 10) || 1);
  const fallbackSuggested = getConservativeSuggestedWorkers(cpuCount);
  const fallbackMax = getConservativeMaxWorkers(cpuCount, fallbackSuggested);
  const maxWorkers = Math.max(1, Number.parseInt(String(data?.max_workers || fallbackMax), 10) || fallbackMax);
  const suggestedWorkers = Math.max(1, Math.min(
    maxWorkers,
    Number.parseInt(String(data?.suggested_workers || fallbackSuggested), 10) || fallbackSuggested
  ));

  return {
    ...defaultSystemResources,
    ...(data || {}),
    cpu_count: cpuCount,
    max_workers: maxWorkers,
    suggested_workers: suggestedWorkers,
    running_workers: Math.max(0, Number.parseInt(String(data?.running_workers || 0), 10) || 0),
    available_workers: Math.max(0, Number.parseInt(String(data?.available_workers ?? Math.max(0, maxWorkers)), 10) || 0),
    active_task_count: Math.max(0, Number.parseInt(String(data?.active_task_count || 0), 10) || 0),
    queued_task_count: Math.max(0, Number.parseInt(String(data?.queued_task_count || 0), 10) || 0),
    active_tasks: Array.isArray(data?.active_tasks) ? data.active_tasks : [],
  };
}

function getParallelWorkerOptions(systemResources) {
  const info = normalizeSystemResources(systemResources);
  return Array.from({ length: info.max_workers }, (_, idx) => {
    const value = idx + 1;
    const marks = [];
    if (value === info.suggested_workers) marks.push('建议');
    if (value === info.max_workers) marks.push('上限');
    return {
      value,
      label: marks.length ? `${value}（${marks.join('/')}）` : String(value),
    };
  });
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
    io_role: 'auto',
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
        padding: '7px 13px',
        borderRadius: 999,
        background: bg,
        color,
        fontWeight: 800,
        fontSize: 14,
      }}
    >
      {status}
    </span>
  );
}

function isTerminalTaskStatus(status) {
  return ['success', 'failed', 'cancelled'].includes(status);
}

function isActiveTaskStatus(status) {
  return ['queued', 'running'].includes(status);
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
function cleanLogLine(line) {
  return String(line || '')
    .replace(/\uFFFD/g, '')
    .replace(/�/g, '')
    .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '')
    .trim();
}

function parseTqdmProgressLine(line) {
  const text = cleanLogLine(line);

  // 兼容：
  // [STDERR]  4%|xx | 1/27 [01:25<37:04, 85.55s/it]
  // [STDERR] 11%|   | 3/27 [04:06<32:43, 81.81s/it]
  const match = text.match(
    /(?:\[(?:STDERR|STDOUT)\]\s*)?(\d{1,3})%\|.*?\|\s*(\d+)\s*\/\s*(\d+)(?:\s*\[([^\]]+)\])?/
  );

  if (!match) return null;

  const percent = Math.max(0, Math.min(100, Number.parseInt(match[1], 10) || 0));
  const current = Number.parseInt(match[2], 10) || 0;
  const total = Number.parseInt(match[3], 10) || 0;
  const detail = match[4] || '';

  return {
    percent,
    current,
    total,
    detail,
  };
}

function getTaskProgressFromLogs(logs) {
  if (!Array.isArray(logs)) return null;

  for (let i = logs.length - 1; i >= 0; i -= 1) {
    const progress = parseTqdmProgressLine(logs[i]);
    if (progress) return progress;
  }

  return null;
}

function isTqdmProgressLog(line) {
  return !!parseTqdmProgressLine(line);
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
  const running = task && isActiveTaskStatus(task.status);
  const taskLogs = Array.isArray(task?.logs)
    ? task.logs
    : task?.logs
      ? [String(task.logs)]
      : [];
  const taskProgress = getTaskProgressFromLogs(taskLogs);

  const visibleLogs = taskLogs
    .map(cleanLogLine)
    .filter((line) => line && !isTqdmProgressLog(line));
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
        borderRadius: 14,
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
          padding: '10px 14px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <div style={{ fontWeight: 800 }}>{win.title}</div>
        <div style={{ display: 'flex', gap: 8 }}>
          {running ? (
            <button style={{ ...styles.topBtn, padding: '6px 10px' }} onClick={() => onMin(win.id)}>
              最小化
            </button>
          ) : (
            <button style={{ ...styles.topBtn, padding: '6px 10px' }} onClick={() => onClose(win.id)}>
              关闭
            </button>
          )}
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
          {task?.status === 'queued' && (
            <div><strong>排队：</strong>{task?.queue_position ? `第 ${task.queue_position} 位` : '等待中'}{task?.queue_reason ? `，${task.queue_reason}` : ''}</div>
          )}
        </div>
        {taskProgress && (
            <div
              style={{
                marginTop: 12,
                padding: 12,
                borderRadius: 12,
                background: '#ffffff',
                border: '1px solid #d8e6f5',
                boxShadow: '0 6px 16px rgba(13, 79, 146, 0.08)',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  marginBottom: 8,
                  fontSize: 13,
                  color: '#17406b',
                  fontWeight: 800,
                }}
              >
                <span>处理进度</span>
                <span>{taskProgress.percent}%</span>
              </div>

              <div
                style={{
                  height: 10,
                  borderRadius: 999,
                  background: '#e6eef8',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${taskProgress.percent}%`,
                    height: '100%',
                    borderRadius: 999,
                    background: 'linear-gradient(135deg, #2d7cf6 0%, #37b6ff 100%)',
                    transition: 'width 0.35s ease',
                  }}
                />
              </div>

              <div
                style={{
                  marginTop: 8,
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 8,
                  fontSize: 12,
                  color: '#5f7088',
                }}
              >
                <span>
                  {taskProgress.current}/{taskProgress.total}
                </span>
                <span style={{ textAlign: 'right' }}>
                  {taskProgress.detail || '正在处理'}
                </span>
              </div>
            </div>
          )}
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
              fontFamily: 'Consolas, "Microsoft YaHei UI", monospace',
              lineHeight: 1.45,
            }}
          >
            {visibleLogs.length ? visibleLogs.join('\n') : '暂无日志'}
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

function TaskTrayFloatingWindow({ count, children, minimized, onToggleMinimize }) {
  const trayWidth = 300;
  const trayHeight = 360;
  const trayMargin = 20;

  const [dragged, setDragged] = useState(false);
  const [pos, setPos] = useState({ left: 0, top: 0 });
  const trayRef = useRef(null);
  const dragRef = useRef(null);

  // 每次从“图标状态”展开时，重新回到右下角
  useEffect(() => {
    if (!minimized) {
      setDragged(false);
    }
  }, [minimized]);

  function onMouseDown(e) {
    if (e.button !== 0) return;

    const rect = trayRef.current?.getBoundingClientRect();
    if (!rect) return;

    setDragged(true);

    dragRef.current = {
      x: e.clientX,
      y: e.clientY,
      left: rect.left,
      top: rect.top,
    };

    function onMoveDoc(ev) {
      if (!dragRef.current) return;

      const dx = ev.clientX - dragRef.current.x;
      const dy = ev.clientY - dragRef.current.y;

      setPos({
        left: Math.max(
          8,
          Math.min(window.innerWidth - trayWidth - 8, dragRef.current.left + dx)
        ),
        top: Math.max(
          8,
          Math.min(window.innerHeight - 80, dragRef.current.top + dy)
        ),
      });
    }

    function onUpDoc() {
      dragRef.current = null;
      document.removeEventListener('mousemove', onMoveDoc);
      document.removeEventListener('mouseup', onUpDoc);
    }

    document.addEventListener('mousemove', onMoveDoc);
    document.addEventListener('mouseup', onUpDoc);
  }

  // 最小化后只显示右下角图标
  if (minimized) {
    return (
      <button
        onClick={onToggleMinimize}
        title="展开任务托盘"
        style={{
          position: 'fixed',
          right: 16,
          bottom: 16,
          width: 54,
          height: 54,
          borderRadius: 16,
          border: '1px solid rgba(255,255,255,0.45)',
          background: 'linear-gradient(135deg,#0d4f92 0%,#1565c0 55%,#2c8ae8 100%)',
          color: '#fff',
          fontSize: 22,
          fontWeight: 900,
          cursor: 'pointer',
          zIndex: 6600,
          boxShadow: '0 16px 36px rgba(5,25,55,0.26)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        ≡
      </button>
    );
  }

  return (
    <div
      ref={trayRef}
      style={{
        position: 'fixed',

        // 没拖动过：强制右下角
        ...(dragged
          ? {
              left: pos.left,
              top: pos.top,
            }
          : {
              right: trayMargin,
              bottom: trayMargin,
            }),

        width: trayWidth,
        maxHeight: 'min(430px, calc(100vh - 90px))',
        zIndex: 6500,
        borderRadius: 16,
        overflow: 'hidden',
        background: 'rgba(255,255,255,0.98)',
        border: '1px solid rgba(255,255,255,0.45)',
        boxShadow: '0 18px 46px rgba(5,25,55,0.26)',
      }}
    >
      <div
        onMouseDown={onMouseDown}
        style={{
          cursor: 'move',
          background: 'linear-gradient(135deg,#0d4f92 0%,#1565c0 55%,#2c8ae8 100%)',
          color: '#fff',
          padding: '10px 12px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          userSelect: 'none',
        }}
      >
        <div style={{ fontWeight: 900, fontSize: 16 }}>任务托盘</div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ fontSize: 12, opacity: 0.92 }}>{count} 个</div>
          <button
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              onToggleMinimize();
            }}
            title="最小化任务托盘"
            style={{
              border: '1px solid rgba(255,255,255,0.35)',
              background: 'rgba(255,255,255,0.12)',
              color: '#fff',
              borderRadius: 6,
              padding: '2px 10px',
              cursor: 'pointer',
              fontSize: 16,
              fontWeight: 800,
              lineHeight: 1,
            }}
          >
            –
          </button>
        </div>
      </div>

      <div
        style={{
          padding: 12,
          maxHeight: 'calc(min(430px, calc(100vh - 90px)) - 44px)',
          overflow: 'auto',
        }}
      >
        {children}
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
    boxShadow: '0 28px 90px rgba(0,0,0,0.36)',
    background: 'rgba(255,255,255,0.10)',
    border: '1px solid rgba(255,255,255,0.20)',
    backdropFilter: 'blur(6px)',
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
            backgroundImage:
                'linear-gradient(135deg, rgba(5, 22, 48, 0.78), rgba(4, 38, 72, 0.58)), url("/images/login-bg.png")',
            backgroundSize: 'cover',
            backgroundPosition: 'center',
            backgroundRepeat: 'no-repeat',
            backgroundAttachment: 'fixed',
            padding: 20,
          }}
      >
        <div style={outerCardStyle}>
          {/* 左侧介绍区 */}
            <div
              style={{
                position: 'relative',
                padding: '48px 42px',
                color: '#fff',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                backgroundImage:
                  'linear-gradient(180deg, rgba(4, 18, 42, 0.35), rgba(4, 18, 42, 0.68)), url("/images/login-left-hero.png")',
                backgroundSize: 'cover',
                backgroundPosition: 'center bottom',
                backgroundRepeat: 'no-repeat',
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

              <h1 style={{fontSize: 42, lineHeight: 1.25, margin: 0, fontWeight: 800}}>
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
                background: 'rgba(248,251,255,0.90)',
                backdropFilter: 'blur(10px)',
                padding: '52px 42px',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
              }}
          >
            <div style={{width: '100%', maxWidth: 420}}>
              <div style={{marginBottom: 18}}>
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
                    <div style={{marginBottom: 10}}>
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
                                setLoginForm({...loginForm, username: e.target.value})
                            }
                            placeholder="请输入用户名"
                            style={fieldInput}
                        />
                        <span style={suffixText}>账号</span>
                      </div>

                      <div style={{...fieldWrap, marginTop: 14}}>
                        <input
                            type={showPassword ? 'text' : 'password'}
                            value={loginForm.password}
                            onChange={(e) =>
                                setLoginForm({...loginForm, password: e.target.value})
                            }
                            placeholder="输入密码"
                            style={fieldInput}
                        />
                        <button
                            type="button"
                            style={{...linkBtn, color: '#8fa0b4'}}
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
                          style={{...widePrimaryBtn, marginTop: 20}}
                          onClick={handleLogin}
                      >
                        登 录
                      </button>

                      <div style={{textAlign: 'center', marginTop: 14}}>
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
                      <div style={{display: 'grid', gap: 12}}>
                        <div style={fieldWrap}>
                          <input
                              value={registerForm.username}
                              onChange={(e) =>
                                  setRegisterForm({...registerForm, username: e.target.value})
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
                                  setRegisterForm({...registerForm, password: e.target.value})
                              }
                              placeholder="请输入密码"
                              style={fieldInput}
                          />
                          <button
                              type="button"
                              style={{...linkBtn, color: '#8fa0b4'}}
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
                              style={{...linkBtn, color: '#8fa0b4'}}
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
                          style={{...widePrimaryBtn, marginTop: 20}}
                          onClick={handleRegister}
                      >
                        注 册
                      </button>
                    </>
                )}

                {/* 找回密码 */}
                {authMode === 'forgot' && (
                    <>
                      <div style={{display: 'grid', gap: 12}}>
                        <div style={fieldWrap}>
                          <input
                              value={forgotForm.username}
                              onChange={(e) =>
                                  setForgotForm({...forgotForm, username: e.target.value})
                              }
                              placeholder="请输入用户名"
                              style={fieldInput}
                          />
                        </div>

                        <button
                            style={{...styles.whiteBtn, width: '100%'}}
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
                                  setForgotForm({...forgotForm, answer: e.target.value})
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
                              style={{...linkBtn, color: '#8fa0b4'}}
                              onClick={() => setShowForgotPassword((v) => !v)}
                          >
                            {showForgotPassword ? '隐藏' : '显示'}
                          </button>
                        </div>
                      </div>

                      <button
                          style={{...widePrimaryBtn, marginTop: 20}}
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
const TASK_TRAY_WIDTH = 300;
const TASK_TRAY_RIGHT = 12;
const TASK_TRAY_BOTTOM = 12;
const TASK_TRAY_RESERVED_RIGHT = TASK_TRAY_WIDTH + TASK_TRAY_RIGHT + 24;
const TASK_TRAY_RESERVED_BOTTOM = 150;

export default function App() {
  const [currentUser, setCurrentUser] = useState(null);
  const [authMode, setAuthMode] = useState('login');
  const [moduleFolderPath, setModuleFolderPath] = useState('');
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
  const [systemResources, setSystemResources] = useState(defaultSystemResources);
  const [users, setUsers] = useState([]);
  const [toolbars, setToolbars] = useState(DEFAULT_TOOLBARS);
  const [dataFiles, setDataFiles] = useState([]);
  const [dataPreview, setDataPreview] = useState(null);
  const [dataPreviewLoading, setDataPreviewLoading] = useState(false);

  const [activeTab, setActiveTab] = useState(() => getSavedActiveTab() || 'tool:cloud');
  const [activeModuleByTool, setActiveModuleByTool] = useState({});
  const [expandedToolTypes, setExpandedToolTypes] = useState({ cloud: true, aerosol: true });
  const [cloudForms, setCloudForms] = useState({});

  const [runtimeForms, setRuntimeForms] = useState({});
  const [moduleForm, setModuleForm] = useState(emptyModuleForm);
  const [editingModuleId, setEditingModuleId] = useState('');
  const [inputEditorOpen, setInputEditorOpen] = useState(false);
  const [inputEditorFields, setInputEditorFields] = useState([]);
  const [uploadToolType, setUploadToolType] = useState('');
  const [dropInfo, setDropInfo] = useState({ drop_dir: '', items: [] });
  const [uploadMsg, setUploadMsg] = useState('');
  const [cppValidation, setCppValidation] = useState(null);
  const [cppValidationLoading, setCppValidationLoading] = useState(false);
  const [moduleMgmtAction, setModuleMgmtAction] = useState('cpp_upload');
  const [pythonSourceDir, setPythonSourceDir] = useState('');
  const [pythonParamJsonPath, setPythonParamJsonPath] = useState('');
  const [pythonModuleId, setPythonModuleId] = useState('');
  const [pythonModuleName, setPythonModuleName] = useState('');
  const [pythonEntryFile, setPythonEntryFile] = useState('main.py');
  const [pythonModuleConfigPath, setPythonModuleConfigPath] = useState('');
  const [pythonModuleConfigPreview, setPythonModuleConfigPreview] = useState(null);
  const [pythonParamInputs, setPythonParamInputs] = useState([]);
  const [pythonUploadMsg, setPythonUploadMsg] = useState('');
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
  const [taskTrayMinimized, setTaskTrayMinimized] = useState(false);
  const zRef = useRef(2000);
  const pollTimerRef = useRef(null);

  const isAdmin = currentUser?.role === 'admin';
  const minimizedTaskCount = windows.filter((w) => w.minimized).length;

  const taskTrayReserveStyle = {
    boxSizing: 'border-box',
  };

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
    arr.push({ key: 'data_mgmt', label: '数据管理' });
    arr.push({ key: 'tasks', label: '任务管理' });
    return arr;
  }, [isAdmin, visibleToolbars]);


  useEffect(() => {
    const init = async () => {
      if (!getAuthToken()) return;
      try {
        const me = await getMe();
        setCurrentUser(me);
        setActiveTab(getSavedActiveTab() || 'tool:cloud');

        const [toolbarList, mods, taskList, dataList, resources] = await Promise.all([
          getToolbars(),
          me.role === 'admin' ? getAdminModules() : getModules(),
          getTasks(),
          listDataFiles(),
          getSystemResources().catch(() => defaultSystemResources),
        ]);

        setDataFiles(Array.isArray(dataList) ? dataList : []);
        setToolbars(Array.isArray(toolbarList) ? toolbarList : DEFAULT_TOOLBARS);
        setModules(Array.isArray(mods) ? mods : []);
        setTasks(Array.isArray(taskList) ? taskList : []);
        setSystemResources(normalizeSystemResources(resources));

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
      const init = { task_name: m.name, _parallel_workers: systemResources.suggested_workers || 1 };
      (m.inputs || []).filter((f) => isFieldVisibleToUser(f) && !isParallelWorkerField(f)).forEach((f) => {
        init[f.key] = f.default ?? '';
      });
      return { ...prev, [m.id]: init };
    });
  });
}, [modules, systemResources.suggested_workers]);

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

  // 工具栏还没加载完成时，不要把 tool:cloud 错误切到任务管理或模块管理
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
  if (!currentUser) return;
  saveActiveTab(activeTab);
  }, [currentUser, activeTab]);
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
        const [latestTasks, resources] = await Promise.all([
          getTasks(),
          getSystemResources().catch(() => null),
        ]);
        setTasks(Array.isArray(latestTasks) ? latestTasks : []);
        if (resources) setSystemResources(normalizeSystemResources(resources));
        await refreshDataFiles();

        for (const w of windows) {
            if (!w.taskId) continue;

            try {
              const detail = await getTask(w.taskId);

              const oldStatus = w.task?.status;
              const newStatus = detail?.status;

              const justFinished =
                isActiveTaskStatus(oldStatus) && isTerminalTaskStatus(newStatus);

              const shouldPopupFinishedWindow = justFinished && w.minimized;

              if (shouldPopupFinishedWindow) {
                setTaskTrayMinimized(false);
              }

              setWindows((prev) =>
                prev.map((x) => {
                  if (x.id !== w.id) return x;

                  if (shouldPopupFinishedWindow) {
                    zRef.current += 1;
                    const { left, top } = getCenteredTaskWindowPosition(0);

                    return {
                      ...x,
                      task: detail,
                      minimized: false,
                      left,
                      top,
                      zIndex: zRef.current,
                    };
                  }

                  if (justFinished) {
                    zRef.current += 1;
                    return {
                      ...x,
                      task: detail,
                      zIndex: zRef.current,
                    };
                  }

                  return {
                    ...x,
                    task: detail,
                  };
                })
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
    const minimizedCount = windows.filter((w) => w.minimized).length;
    if (minimizedCount === 0) {
      setTaskTrayMinimized(false);
    }
  }, [windows]);

  async function handleLogin() {
    try {
      setLoginError('');
      const data = await login(loginForm.username, loginForm.password, loginType);
      setAuthToken(data.token);
      setCurrentUser(data.user);
      setActiveTab('tool:cloud');
      saveActiveTab('tool:cloud');

      const [toolbarList, mods, taskList, dataList, resources] = await Promise.all([
        getToolbars(),
        data.user.role === 'admin' ? getAdminModules() : getModules(),
        getTasks(),
        listDataFiles(),
        getSystemResources().catch(() => defaultSystemResources),
      ]);

      setToolbars(Array.isArray(toolbarList) ? toolbarList : DEFAULT_TOOLBARS);
      setModules(Array.isArray(mods) ? mods : []);
      setTasks(Array.isArray(taskList) ? taskList : []);
      setDataFiles(Array.isArray(dataList) ? dataList : []);
      setSystemResources(normalizeSystemResources(resources));

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
  async function browseModuleFolder() {
  try {
    const result = await chooseLocalDir({
      title: '选择 C++ 可执行模块文件夹',
    });

    if (result?.path) {
      setModuleFolderPath(result.path);
      setCppValidation(null);
      await validateCppModuleFolderPath(result.path, { silent: false });
    }
  } catch (e) {
    setUploadMsg(e?.message || '选择模块文件夹失败');
  }
}
async function browsePythonModuleConfigJson() {
  try {
    const result = await chooseLocalFile({
      title: '选择 Python 模块配置 JSON',
      filetypes: [['JSON 文件', '*.json'], ['All Files', '*.*']],
    });

    if (!result?.path) return;

    setPythonModuleConfigPath(result.path);
    setPythonUploadMsg('正在解析 Python 模块配置 JSON...');

    const data = await parsePythonModuleConfig(result.path);

    setPythonModuleConfigPreview(data?.module || null);
    setPythonParamInputs(Array.isArray(data?.inputs) ? data.inputs : []);
    setPythonUploadMsg(`已识别 ${Array.isArray(data?.inputs) ? data.inputs.length : 0} 个参数`);
  } catch (e) {
    setPythonModuleConfigPath('');
    setPythonModuleConfigPreview(null);
    setPythonParamInputs([]);
    setPythonUploadMsg(e?.message || '解析 Python 模块配置 JSON 失败');
  }
}
async function validateCppModuleFolderPath(pathValue = moduleFolderPath, options = {}) {
  const path = String(pathValue || '').trim();
  if (!path) {
    setUploadMsg('请选择 C++ 可执行模块文件夹');
    setCppValidation(null);
    return null;
  }

  if (!uploadToolType) {
    alert('请先选择模块所属工具栏');
    return null;
  }

  setCppValidationLoading(true);
  if (!options.silent) setUploadMsg('正在检查 C++ 模块规范...');

  try {
    const data = await validateCppModuleFolder({
      folder_path: path,
      tool_type: uploadToolType,
      auto_collect_dependencies: true,
    });
    setCppValidation(data);

    const errorCount = Array.isArray(data?.errors) ? data.errors.length : 0;
    const warningCount = Array.isArray(data?.warnings) ? data.warnings.length : 0;
    const missingCount = Array.isArray(data?.missing_files) ? data.missing_files.length : 0;

    if (data?.can_install) {
      setUploadMsg(`C++ 模块检查通过：错误 0 个，警告 ${warningCount} 个，缺失 ${missingCount} 个。可以安装。`);
    } else {
      setUploadMsg(`C++ 模块检查未通过：错误 ${errorCount} 个，警告 ${warningCount} 个，缺失 ${missingCount} 个。请先按提示修改。`);
    }

    return data;
  } catch (e) {
    setCppValidation(null);
    setUploadMsg(e?.message || 'C++ 模块检查失败');
    return null;
  } finally {
    setCppValidationLoading(false);
  }
}

async function installModuleFolder() {
  if (!moduleFolderPath.trim()) {
    setUploadMsg('请选择 C++ 可执行模块文件夹');
    return;
  }

  if (!uploadToolType) {
    alert('请先选择模块所属工具栏');
    return;
  }

  const validation = await validateCppModuleFolderPath(moduleFolderPath, { silent: true });
  if (!validation?.can_install) {
    setUploadMsg('C++ 模块没有通过检查，已阻止安装。请根据下方错误、缺失文件和修改建议处理后再安装。');
    return;
  }

  setUploadMsg('正在安装 C++ 可执行模块，并尝试自动收集运行时 DLL 依赖...');

  try {
    await uploadModuleFolder({
      folder_path: moduleFolderPath.trim(),
      tool_type: uploadToolType,
      runtime: 'cpp_native',
      auto_collect_dependencies: true,
    });

    setModuleFolderPath('');
    setCppValidation(null);
    setUploadMsg('C++ 可执行模块安装成功');

    await Promise.all([refreshModules(), refreshToolbars(), refreshDropZipList()]);
  } catch (e) {
    setUploadMsg(e?.message || 'C++ 可执行模块安装失败');
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
    const [list, resources] = await Promise.all([
      getTasks(),
      getSystemResources().catch(() => null),
    ]);
    setTasks(Array.isArray(list) ? list : []);
    if (resources) setSystemResources(normalizeSystemResources(resources));
  }
  async function refreshDataFiles() {
    const list = await listDataFiles();
    setDataFiles(Array.isArray(list) ? list : []);
  }
function getCenteredTaskWindowPosition(offset = 0) {
  const popupWidth = 420;
  const popupHeight = 520;

  return {
    left: Math.max(16, (window.innerWidth - popupWidth) / 2 + offset),
    top: Math.max(90, (window.innerHeight - popupHeight) / 2 + offset),
  };
}
function addTaskWindow(task, title) {
  zRef.current += 1;
  setWindows((prev) => {
    const offset = (prev.length % 4) * 24;
    const popupWidth = 420;
    const popupHeight = 520;

    const left = Math.max(16, (window.innerWidth - popupWidth) / 2 + offset);
    const top = Math.max(90, (window.innerHeight - popupHeight) / 2 + offset);

    return [
      ...prev,
      {
        id: `w_${task.id}`,
        taskId: task.id,
        task,
        title,
        minimized: false,
        left,
        top,
        zIndex: zRef.current,
      },
    ];
  });
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
      await refreshTasks();
      await refreshDataFiles();
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
      const parallelWorkers = clampParallelWorkersValue(form._parallel_workers, systemResources.max_workers);
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


function renderValidationItems(title, items, color = '#4f6682') {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontWeight: 900, color, marginBottom: 6 }}>{title}：{items.length} 项</div>
      <div style={{ display: 'grid', gap: 6 }}>
        {items.map((item, idx) => (
          <div
            key={`${title}_${idx}`}
            style={{
              border: '1px solid #d7e3f0',
              background: '#fff',
              borderRadius: 10,
              padding: '8px 10px',
              fontSize: 13,
              lineHeight: 1.65,
              color: '#37536f',
            }}
          >
            {typeof item === 'object' && item ? (
              <>
                <div><strong>{item.field || item.path || `第 ${idx + 1} 项`}</strong></div>
                <div>{item.message || item.reason || ''}</div>
                {item.suggestion && <div style={{ color: '#64748b' }}>建议：{item.suggestion}</div>}
              </>
            ) : (
              <div>{String(item)}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function renderCppValidationReport() {
  if (!cppValidation) return null;
  const dep = cppValidation.dependency_report || {};
  const canInstall = !!cppValidation.can_install;

  return (
    <div
      style={{
        border: '1px solid #d7e3f0',
        borderRadius: 14,
        background: canInstall ? 'rgba(34,197,94,0.06)' : 'rgba(239,68,68,0.06)',
        padding: 14,
        color: '#173353',
        lineHeight: 1.75,
      }}
    >
      <div style={{ fontWeight: 900, color: canInstall ? '#1f7f36' : '#b42318', marginBottom: 6 }}>
        {canInstall ? 'C++ 模块检查通过，可以安装' : 'C++ 模块检查未通过，请先修改'}
      </div>
      <div style={{ fontSize: 13, color: '#4f6682', wordBreak: 'break-all' }}>
        module.json：{cppValidation.module_json_path || '-'}
      </div>
      <div style={{ fontSize: 13, color: '#4f6682', wordBreak: 'break-all' }}>
        模块根目录：{cppValidation.module_root || '-'}
      </div>

      {cppValidation.module && (
        <div style={{ marginTop: 8, fontSize: 13 }}>
          <strong>识别模块：</strong>
          {cppValidation.module.name || '-'}（{cppValidation.module.id || '-'}）
        </div>
      )}

      {renderValidationItems('错误', cppValidation.errors, '#b42318')}
      {renderValidationItems('缺少文件/文件夹', cppValidation.missing_files, '#b45309')}
      {renderValidationItems('警告', cppValidation.warnings, '#815b00')}
      {renderValidationItems('修改建议', cppValidation.suggestions, '#235ed8')}

      <div style={{ marginTop: 12, borderTop: '1px solid #d7e3f0', paddingTop: 10 }}>
        <div style={{ fontWeight: 900, color: '#12385f', marginBottom: 6 }}>运行时依赖检查</div>
        <div style={{ fontSize: 13, color: '#4f6682' }}>
          分析器：{dep.analyzer || '未识别'}；目标目录：{dep.target_dir || 'deps/auto'}
        </div>
        {dep.message && <div style={{ fontSize: 13, color: '#4f6682' }}>{dep.message}</div>}
        {Array.isArray(dep.copied) && dep.copied.length > 0 && (
          <div style={{ fontSize: 13, color: '#1f7f36' }}>可自动复制：{dep.copied.join(', ')}</div>
        )}
        {Array.isArray(dep.missing_imports) && dep.missing_imports.length > 0 && (
          <div style={{ fontSize: 13, color: '#b45309' }}>
            未找到 DLL：{dep.missing_imports.join(', ')}
          </div>
        )}
      </div>
    </div>
  );
}

function renderModuleMgmtButton(key, title, desc, onClick) {
  const active = moduleMgmtAction === key;

  return (
    <button
      type="button"
      onClick={() => {
        setModuleMgmtAction(key);
        onClick?.();
      }}
      style={{
        width: '100%',
        textAlign: 'left',
        border: active ? '2px solid #2d7cf6' : '1px solid #d7e3f1',
        background: active
          ? 'linear-gradient(135deg, rgba(45,124,246,0.12), rgba(45,124,246,0.04))'
          : '#fff',
        borderRadius: 14,
        padding: '18px 18px',
        cursor: 'pointer',
        boxShadow: active ? '0 10px 22px rgba(45,124,246,0.12)' : 'none',
      }}
    >
      <div style={{ fontSize: 18, fontWeight: 900, color: '#12385f', marginBottom: 8 }}>
        {title}
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.6, color: '#6a7f96' }}>
        {desc}
      </div>
    </button>
  );
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
        const formatFailure = (item) => {
          const err = item?.error;
          if (err && typeof err === 'object') {
            const parts = [];
            if (err.message) parts.push(err.message);
            if (Array.isArray(err.errors) && err.errors.length) {
              parts.push('错误：' + err.errors.map((e) => `${e.field || '-'}：${e.message || ''}${e.suggestion ? `；建议：${e.suggestion}` : ''}`).join('；'));
            }
            if (Array.isArray(err.missing_files) && err.missing_files.length) {
              parts.push('缺少：' + err.missing_files.map((e) => `${e.path || '-'}${e.reason ? `：${e.reason}` : ''}`).join('；'));
            }
            if (Array.isArray(err.warnings) && err.warnings.length) {
              parts.push('警告：' + err.warnings.map((e) => `${e.field || '-'}：${e.message || ''}`).join('；'));
            }
            return `${item.name}: ${parts.join('\n') || JSON.stringify(err, null, 2)}`;
          }
          return `${item.name}: ${String(err || '未知错误')}`;
        };
        alert((data.failed || []).map(formatFailure).join('\n\n'));
      }
    } catch (e) {
      setUploadMsg(e?.message || '本地目录安装失败');
    }
  }
async function uploadPythonConfigJson() {
  if (!pythonModuleConfigPath.trim()) {
    setPythonUploadMsg('请选择 Python 模块配置 JSON');
    return;
  }

  setPythonUploadMsg('正在读取配置 JSON、创建独立 Python 环境并安装模块，请稍等...');

  try {
    await uploadPythonModuleConfig(pythonModuleConfigPath.trim());

    setPythonModuleConfigPath('');
    setPythonModuleConfigPreview(null);
    setPythonParamInputs([]);
    setPythonUploadMsg('');

    await Promise.all([refreshModules(), refreshToolbars(), refreshDropZipList()]);

    alert('Python 模块已根据配置 JSON 安装成功');
  } catch (e) {
    setPythonUploadMsg(e?.message || 'Python 模块配置 JSON 安装失败');
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
      next.io_role = ['input', 'output'].includes(String(next.io_role || '').toLowerCase())
        ? String(next.io_role).toLowerCase()
        : 'auto';
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
  function renderModuleRuntime(module) {

    if (!module) {
      return <div style={{ padding: 20 }}>当前没有匹配到可运行模块</div>;
    }

    const form = runtimeForms[module.id] || {
      task_name: module.name,
      _parallel_workers: systemResources.suggested_workers || 1,
    };
    const resourceInfo = normalizeSystemResources(systemResources);
    const parallelWorkerOptions = getParallelWorkerOptions(resourceInfo);
    const selectedParallelWorkers = clampParallelWorkersValue(
      form._parallel_workers || resourceInfo.suggested_workers || 1,
      resourceInfo.max_workers
    );

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
              value={selectedParallelWorkers}
              onChange={(e) =>
                setRuntimeForms((prev) => ({
                  ...prev,
                  [module.id]: {
                    ...prev[module.id],
                    _parallel_workers: clampParallelWorkersValue(e.target.value, resourceInfo.max_workers),
                  },
                }))
              }
              style={styles.input}
            >
              {parallelWorkerOptions.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>

            <div
              style={{
                marginTop: 10,
                padding: 12,
                borderRadius: 12,
                background: 'rgba(45,124,246,0.06)',
                border: '1px solid rgba(45,124,246,0.13)',
                color: '#45627f',
                fontSize: 13,
                lineHeight: 1.7,
              }}
            >
              <div>本机 CPU 核数：<strong>{resourceInfo.cpu_count}</strong>；建议进程数：<strong>{resourceInfo.suggested_workers}</strong>；上限进程数：<strong>{resourceInfo.max_workers}</strong></div>
              <div style={{ marginTop: 4 }}>建议值按内存较重的模块保守计算：约 CPU 核数 1/3，最高 8；上限约 CPU 核数 1/2，最高 12。</div>
              <div>当前已占用进程槽：<strong>{resourceInfo.running_workers}/{resourceInfo.max_workers}</strong>；等待队列：<strong>{resourceInfo.queued_task_count}</strong></div>
              <div>系统 CPU 使用率：<strong>{resourceInfo.cpu_percent == null ? '-' : `${Number(resourceInfo.cpu_percent).toFixed(1)}%`}</strong>；模块进程 CPU：<strong>{resourceInfo.running_process_cpu_percent == null ? '-' : `${Number(resourceInfo.running_process_cpu_percent).toFixed(1)}%`}</strong></div>
              <div style={{ marginTop: 4 }}>超过上限或 CPU 负载较高时，任务会自动进入排队状态；排队任务可在任务管理里取消。</div>
            </div>
          </label>

          {(module.inputs || []).filter((f) => isFieldVisibleToUser(f) && !isParallelWorkerField(f)).map((field) => (
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
          gridTemplateColumns: '300px minmax(0, 1fr) 280px',
          gap: 12,
          minHeight: 'calc(100vh - 98px)',
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
          {selectedModule
              ? renderModuleRuntime(selectedModule)
              : <div style={{ padding: 20, color: '#999' }}>当前工具栏暂无可运行模块</div>}
        </div>


      </section>
    );
  }
function renderTaskTrayPanel() {
  const minimizedWindows = windows.filter((w) => w.minimized);

  return (
    <div style={{ display: 'grid', gap: 8 }}>
      {minimizedWindows.length === 0 && (
        <div
          style={{
            color: '#6b8097',
            fontSize: 13,
            lineHeight: 1.6,
            padding: '8px 2px',
          }}
        >
          当前无最小化任务
        </div>
      )}

      {minimizedWindows.map((w) => {
        const terminal = isTerminalTaskStatus(w.task?.status);
        const trayTaskId = w.task?.id || w.taskId || '';
        const trayTitle = trayTaskId ? `${w.title} · ${trayTaskId}` : w.title;
        return (
          <div
            key={w.id}
            style={{
              border: '1px solid #d6e2ef',
              background: '#fff',
              borderRadius: 12,
              padding: '10px 12px',
              boxShadow: '0 4px 12px rgba(15,45,80,0.04)',
            }}
          >
            <button
              onClick={() =>
                setWindows((prev) =>
                  prev.map((x) => (x.id === w.id ? { ...x, minimized: false, zIndex: ++zRef.current } : x))
                )
              }
              style={{
                border: 'none',
                background: 'transparent',
                padding: 0,
                margin: 0,
                width: '100%',
                textAlign: 'left',
                cursor: 'pointer',
              }}
            >
              <div
                style={{
                  fontWeight: 800,
                  color: '#12385f',
                  fontSize: 13,
                  lineHeight: 1.35,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={trayTaskId ? `${w.title}（任务ID：${trayTaskId}）` : w.title}
              >
                {trayTitle}
              </div>
              <div style={{ color: '#6a7f96', marginTop: 4, fontSize: 12 }}>
                {w.task?.status || '-'}
              </div>
            </button>

            {terminal && (
              <button
                style={{
                  ...tableDangerBtnStyle,
                  padding: '4px 8px',
                  fontSize: 12,
                  marginTop: 8,
                }}
                onClick={() => setWindows((prev) => prev.filter((x) => x.id !== w.id))}
              >
                关闭
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function renderDataManagementPage() {
  async function handlePreview(file) {
    try {
      setDataPreviewLoading(true);
      const data = await previewDataFile(file.id);
      setDataPreview(data);
    } catch (e) {
      alert(e?.message || '预览失败');
    } finally {
      setDataPreviewLoading(false);
    }
  }

  async function handleReveal(file) {
    try {
      await revealDataFile(file.id);
    } catch (e) {
      alert(e?.message || '打开文件所在位置失败');
    }
  }

  async function handleDelete(file) {
    if (!window.confirm(`确定删除文件：${file.name || file.file_name || file.id} 吗？`)) return;

    try {
      await deleteDataFile(file.id);
      await refreshDataFiles();
    } catch (e) {
      alert(e?.message || '删除失败');
    }
  }

  return (
    <>
      <section
        style={{
          minHeight: 'calc(100vh - 98px)',
          ...taskTrayReserveStyle,
        }}
      >
        <div
          style={{
            ...styles.card,
            padding: 16,
            minWidth: 0,
            overflow: 'hidden',
          }}
        >
          <div style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            marginBottom: 12,
            gap: 12,
          }}>
            <div>
              <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', letterSpacing: '0.2px' }}>
                数据管理
              </div>
              <div style={{ color: '#6a7f96', marginTop: 4, fontSize: 13 }}>
                只展示模块运行成功后登记的输出文件；文件仍保留在原始输出路径，不会被移动。
              </div>
            </div>

            <button style={{ ...styles.whiteBtn, padding: '8px 18px', fontSize: 13 }} onClick={refreshDataFiles}>
              刷新
            </button>
          </div>

          <div style={{
            overflow: 'auto',
            background: '#fff',
            borderRadius: 10,
            border: '1px solid #dfe8f2',
            boxShadow: '0 8px 22px rgba(15, 45, 80, 0.05)',
          }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 1160, tableLayout: 'fixed' }}>
              <thead>
                <tr>
                  <th style={{...thStyle, width: 58}}>文件ID</th>
                  {isAdmin && <th style={{...thStyle, width: 110}}>用户ID</th>}
                  <th style={{...thStyle, width: 250}}>文件名</th>
                  <th style={{...thStyle, width: 78}}>类型</th>
                  <th style={{...thStyle, width: 150}}>所属模块</th>
                  <th style={{...thStyle, width: 88}}>大小</th>
                  <th style={{...thStyle, width: 145}}>创建时间</th>
                  <th style={thStyle}>本地路径</th>
                  <th style={{...thStyle, width: 210}}>操作</th>
                </tr>
              </thead>

              <tbody>
                {dataFiles.length === 0 && (
                  <tr>
                    <td style={tdStyle} colSpan={isAdmin ? 9 : 8}>
                      暂无输出结果文件。运行模块后，系统会自动登记输出路径下的文件。
                    </td>
                  </tr>
                )}

                {dataFiles.map((file, index) => (
                  <tr
                    key={`${file.id}_${file.path}`}
                    style={{
                      background: index % 2 === 0 ? '#f8fbff' : '#ffffff',
                    }}
                  >
                    <td style={tdStyle}>{file.id}</td>

                    {isAdmin && (
                        <td style={tdEllipsisStyle} title={file.owner_username || '-'}>
                          {file.owner_username || '-'}
                        </td>
                    )}

                    <td style={tdEllipsisStyle} title={file.file_name || file.name || '-'}>
                      {file.file_name || file.name || '-'}
                    </td>
                    <td style={tdStyle}>{file.file_type}</td>
                    <td style={tdEllipsisStyle} title={file.module_name || file.module_id || '-'}>
                      {file.module_name || file.module_id}
                    </td>
                    <td style={tdStyle}>{file.size_text || file.size}</td>
                    <td style={tdStyle}>{file.created_at || '-'}</td>
                    <td style={tdEllipsisStyle} title={file.path}>
                      {file.path}
                    </td>
                    <td style={tdStyle}>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'nowrap', alignItems: 'center' }}>
                        <button style={tableActionBtnStyle} onClick={() => handlePreview(file)}>
                          预览
                        </button>
                        <button style={tableActionBtnStyle} onClick={() => handleReveal(file)}>
                          打开位置
                        </button>
                        <button style={taskTableDangerBtnStyle} onClick={() => handleDelete(file)}>
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>


      </section>

      {dataPreview && (
        <SimpleOverlay
          title={`文件预览：${dataPreview.name || ''}`}
          onClose={() => setDataPreview(null)}
          width="min(1100px, 96vw)"
        >
          {dataPreviewLoading && <div>加载中...</div>}

          {dataPreview.type === 'image' && dataPreview.data_url ? (
            <div>
              <div style={{ marginBottom: 12, color: '#6a7f96', wordBreak: 'break-all' }}>
                {dataPreview.path}
              </div>
              <img
                src={dataPreview.data_url}
                alt={dataPreview.name}
                style={{
                  maxWidth: '100%',
                  maxHeight: '75vh',
                  borderRadius: 12,
                  border: '1px solid #d8e3f0',
                  background: '#fff',
                }}
              />
            </div>
          ) : (
            <div style={{ lineHeight: 1.8 }}>
              <div>{dataPreview.message || '该文件暂不支持在线预览'}</div>
              <div style={{ color: '#6a7f96', wordBreak: 'break-all', marginTop: 8 }}>
                {dataPreview.path}
              </div>
            </div>
          )}
        </SimpleOverlay>
      )}
    </>
  );
}

function renderTaskManagementPage() {
  return (
    <section
      style={{
        minHeight: 'calc(100vh - 98px)',
        ...taskTrayReserveStyle,
      }}
    >
      <div
        style={{
          ...styles.card,
          padding: 16,
          minWidth: 0,
          overflow: 'hidden',
        }}
      >
        <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', letterSpacing: '0.2px', marginBottom: 12 }}>
          任务管理
        </div>

        <div style={{
          overflow: 'auto',
          background: '#fff',
          borderRadius: 10,
          border: '1px solid #dfe8f2',
          boxShadow: '0 8px 22px rgba(15, 45, 80, 0.05)',
        }}>
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              minWidth: 900,
              tableLayout: 'fixed',
            }}
          >
            <thead>
            <tr>
              <th style={{...taskThStyle, width: 130}}>任务ID</th>
              {isAdmin && <th style={{...taskThStyle, width: 110}}>用户ID</th>}
              <th style={{...taskThStyle, width: 190}}>模块</th>
              <th style={{...taskThStyle, width: 90}}>类型</th>
              <th style={{...taskThStyle, width: 115}}>状态</th>
              <th style={{...taskThStyle, width: 165}}>开始时间</th>
              <th style={{...taskThStyle, width: 165}}>结束时间</th>
              <th style={{...taskThStyle, width: 150}}>操作</th>
            </tr>
            </thead>

            <tbody>
            {tasks.map((task, index) => (
                <tr
                    key={task.id}
                    style={{
                      background: index % 2 === 0 ? '#f8fbff' : '#ffffff',
                    }}
                >
                  <td style={taskTdEllipsisStyle} title={task.id}>{task.id}</td>

                  {isAdmin && (
                      <td style={taskTdEllipsisStyle} title={task.owner_username || '-'}>
                        {task.owner_username || '-'}
                      </td>
                  )}

                  <td style={taskTdEllipsisStyle} title={task.module_name || '-'}>
                    {task.module_name}
                  </td>
                  <td style={taskTdStyle}>{task.kind}</td>
                  <td style={taskTdStyle}>
                    {statusBadge(task.status)}
                    {task.status === 'queued' && (task.queue_position || task.queue_reason) && (
                      <div style={{ marginTop: 6, fontSize: 12, color: '#6b5aa8', lineHeight: 1.45 }}>
                        {task.queue_position ? `排队第 ${task.queue_position} 位` : '排队中'}
                        {task.queue_reason ? `：${task.queue_reason}` : ''}
                      </div>
                    )}
                  </td>
                  <td style={taskTdStyle}>{task.started_at || '-'}</td>
                  <td style={taskTdStyle}>{task.ended_at || '-'}</td>
                  <td style={taskTdStyle}>
                    <div style={{display: 'flex', gap: 6, flexWrap: 'nowrap', alignItems: 'center' }}>
                      <button
                        style={taskTableActionBtnStyle}
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
                          style={taskTableDangerBtnStyle}
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

                      <button style={taskTableDangerBtnStyle} onClick={() => handleDeleteTask(task.id)}>
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}

              {tasks.length === 0 && (
                <tr>
                  <td colSpan={isAdmin ? 8 : 7} style={{...taskTdStyle, padding: 30, textAlign: 'center', color: '#6c8098'}}>
                    暂无任务
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
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

      <div style={{ padding: 12 }}>
        {activeTab === 'module_mgmt' && isAdmin && (
          <section style={{ ...styles.card, padding: 16, minHeight: 'calc(100vh - 98px)' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '380px minmax(0, 1fr)', gap: 16 }}>
              <div style={{ ...styles.card, padding: 16 }}>
                <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 16 }}>
                  模块管理功能
                </div>

                <div style={{ display: 'grid', gap: 12 }}>
                  {renderModuleMgmtButton(
                    'python_upload',
                    'Python 源代码环境上传',
                    '选择 Python 源码文件夹和参数 JSON，系统自动创建独立环境并注册模块。'
                  )}

                  {renderModuleMgmtButton(
                    'cpp_upload',
                    'C++ 可执行模块上传',
                    '选择包含 module.json、编译好的 exe、resources 和 deps 的 C++ 可执行模块文件夹，先检查规范再安装。'
                  )}
                  {renderModuleMgmtButton(
                    'installed_modules',
                    '已安装模块',
                    '查看当前已经安装到系统中的模块，并进行编辑或删除。'
                  )}

                  {renderModuleMgmtButton(
                    'toolbars',
                    '工具栏',
                    '管理云反演、气溶胶反演等顶部工具栏分类。'
                  )}
                </div>
              </div>

              <div style={{ display: 'grid', gap: 16, minWidth: 0 }}>
                {moduleMgmtAction === 'python_upload' && (
                  <div style={{ ...styles.card, padding: 22 }}>
                    <div style={{ fontSize: 24, fontWeight: 900, color: '#12385f', marginBottom: 10 }}>
                      Python 源代码环境上传
                    </div>

                    <div style={{ color: '#6a7f96', lineHeight: 1.8, marginBottom: 18 }}>
                      选择一个 Python 模块配置 JSON。该 JSON 用来指向 Python 源码文件夹、入口文件和参数 JSON，
                      系统会自动识别参数、创建独立 Python 环境，并注册成可运行模块。
                    </div>

                    <div style={{ display: 'grid', gap: 16, maxWidth: 960 }}>
                      <div>
                        <div style={labelStyle}>Python 模块配置 JSON</div>
                        <div style={{ display: 'flex', gap: 10 }}>
                          <input
                            style={{ ...styles.input, flex: 1 }}
                            value={pythonModuleConfigPath}
                            readOnly
                            placeholder="请选择 python_module.json"
                          />
                          <button style={styles.whiteBtn} onClick={browsePythonModuleConfigJson}>
                            浏览并识别
                          </button>
                        </div>
                      </div>

                      {pythonModuleConfigPreview && (
                        <div
                          style={{
                            border: '1px solid #d7e3f0',
                            borderRadius: 12,
                            background: '#fff',
                            padding: 12,
                            color: '#37536f',
                            lineHeight: 1.8,
                          }}
                        >
                          <div style={{ fontWeight: 900, color: '#12385f', marginBottom: 8 }}>
                            模块配置预览
                          </div>
                          <div>模块 ID：{pythonModuleConfigPreview.module_id}</div>
                          <div>模块名称：{pythonModuleConfigPreview.module_name}</div>
                          <div>所属工具栏：{pythonModuleConfigPreview.tool_type}</div>
                          <div>入口文件：{pythonModuleConfigPreview.entry_file}</div>
                          <div style={{ wordBreak: 'break-all' }}>源码文件夹：{pythonModuleConfigPreview.source_dir}</div>
                          <div style={{ wordBreak: 'break-all' }}>参数 JSON：{pythonModuleConfigPreview.param_json_path || '已内嵌 param_template'}</div>
                        </div>
                      )}

                      {pythonParamInputs.length > 0 && (
                        <div
                          style={{
                            border: '1px solid #d7e3f0',
                            borderRadius: 12,
                            background: '#fff',
                            padding: 12,
                          }}
                        >
                          <div style={{ fontWeight: 900, color: '#12385f', marginBottom: 8 }}>
                            已识别参数：{pythonParamInputs.length} 个
                          </div>

                          <div style={{ display: 'grid', gap: 6 }}>
                            {pythonParamInputs.map((item) => (
                              <div
                                key={item.key}
                                style={{
                                  display: 'grid',
                                  gridTemplateColumns: '1fr 120px 1.5fr',
                                  gap: 10,
                                  fontSize: 13,
                                  color: '#37536f',
                                  borderTop: '1px solid #edf2f7',
                                  paddingTop: 6,
                                }}
                              >
                                <div>{item.label || item.key}</div>
                                <div>{item.type}</div>
                                <div style={{ wordBreak: 'break-all' }}>{String(item.default ?? '')}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                        <button style={styles.blueBtn} onClick={uploadPythonConfigJson}>
                          根据配置 JSON 安装模块
                        </button>

                        <button
                          style={styles.whiteBtn}
                          onClick={() => {
                            setPythonModuleConfigPath('');
                            setPythonModuleConfigPreview(null);
                            setPythonParamInputs([]);
                            setPythonUploadMsg('');
                          }}
                        >
                          清空
                        </button>
                      </div>

                      {pythonUploadMsg && (
                        <div
                          style={{
                            whiteSpace: 'pre-wrap',
                            color:
                              pythonUploadMsg.includes('失败') ||
                              pythonUploadMsg.includes('错误')
                                ? '#bb2c2c'
                                : '#4f6682',
                            lineHeight: 1.7,
                          }}
                        >
                          {pythonUploadMsg}
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {moduleMgmtAction === 'cpp_upload' && (
                    <div style={{ ...styles.card, padding: 18 }}>
                      <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 14 }}>
                        C++ 可执行模块上传
                      </div>

                      <div style={{ color: '#6a7f96', lineHeight: 1.8, marginBottom: 14 }}>
                        请选择一个已经准备好的 C++ 可执行模块文件夹。推荐目录结构为：module.json、编译后的 exe、resources 固定资源目录、deps 运行时 DLL 依赖目录。
                        C++ 模块不需要上传源码；系统会先检查 module.json 是否规范、固定资源是否缺失、exe 是否存在，并尝试识别运行时 DLL 依赖。
                      </div>

                      <div
                        style={{
                          border: '1px solid #d7e3f0',
                          background: 'rgba(45,124,246,0.05)',
                          borderRadius: 12,
                          padding: 12,
                          color: '#37536f',
                          lineHeight: 1.8,
                          marginBottom: 14,
                        }}
                      >
                        <div style={{ fontWeight: 900, color: '#12385f', marginBottom: 6 }}>依赖说明</div>
                        <div>deps 主要放 <strong>运行时 DLL 依赖</strong>，也就是 exe 启动时还需要但没有打进 exe 的动态库。</div>
                        <div>C++ 模块这里按成品 exe 安装，不需要源码、头文件、静态库或 CMake 工程。deps 只放 exe 运行时还缺少的 DLL。</div>
                        <div>自动收集只能尽力识别 exe 导入表中的 DLL；如果程序用 LoadLibrary 动态加载 DLL，仍建议手动放到 deps 并在 module.json 的 dependency_dirs 中声明。</div>
                      </div>

                      <div style={{ display: 'grid', gap: 12 }}>
                        <div>
                          <div style={labelStyle}>模块所属工具栏</div>
                          <select
                            value={uploadToolType}
                            onChange={(e) => {
                              setUploadToolType(e.target.value);
                              setCppValidation(null);
                            }}
                            style={styles.input}
                          >
                            {renderToolbarOptions()}
                          </select>
                        </div>

                        <div>
                          <div style={labelStyle}>C++ 可执行模块文件夹</div>
                          <div style={{ display: 'flex', gap: 10 }}>
                            <input
                              style={{ ...styles.input, flex: 1 }}
                              value={moduleFolderPath}
                              onChange={(e) => {
                                setModuleFolderPath(e.target.value);
                                setCppValidation(null);
                              }}
                              placeholder="请选择或粘贴包含 module.json 和 exe 的 C++ 模块文件夹路径"
                            />
                            <button style={styles.whiteBtn} onClick={browseModuleFolder}>
                              浏览并检查
                            </button>
                          </div>
                        </div>

                        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                          <button
                            style={styles.whiteBtn}
                            onClick={() => downloadTextFile('cpp_module_template.json', getCppExecutableModuleTemplateText())}
                          >
                            下载 module.json 模板
                          </button>

                          <button
                            style={styles.whiteBtn}
                            onClick={async () => {
                              try {
                                await navigator.clipboard.writeText(getCppExecutableModuleTemplateText());
                                setUploadMsg('已复制 C++ 可执行模块 module.json 模板。用户需要把文件命名为 module.json，并放到 exe 同级目录。');
                              } catch {
                                setUploadMsg(getCppExecutableModuleTemplateText());
                              }
                            }}
                          >
                            复制模板内容
                          </button>

                          <button
                            style={styles.whiteBtn}
                            onClick={() => validateCppModuleFolderPath(moduleFolderPath)}
                            disabled={cppValidationLoading}
                          >
                            {cppValidationLoading ? '检查中...' : '检查模块规范'}
                          </button>

                          <button style={styles.blueBtn} onClick={installModuleFolder} disabled={cppValidationLoading}>
                            安装 C++ 可执行模块
                          </button>

                          <button
                            style={styles.whiteBtn}
                            onClick={() => {
                              setModuleFolderPath('');
                              setUploadMsg('');
                              setCppValidation(null);
                            }}
                          >
                            清空
                          </button>

                          <button style={styles.whiteBtn} onClick={refreshDropZipList}>
                            刷新投放目录
                          </button>

                          <button style={styles.whiteBtn} onClick={() => installFromDrop('')}>
                            扫描本地目录安装
                          </button>

                          <button style={styles.whiteBtn} onClick={() => setShowDropHint(true)}>
                            C++ 可执行模块目录说明
                          </button>
                        </div>

                        {uploadMsg && (
                          <div
                            style={{
                              color: uploadMsg.includes('失败') || uploadMsg.includes('未通过') || uploadMsg.includes('阻止') ? '#bb2c2c' : '#4f6682',
                              whiteSpace: 'pre-wrap',
                              lineHeight: 1.7,
                            }}
                          >
                            {uploadMsg}
                          </div>
                        )}

                        {renderCppValidationReport()}

                        {dropInfo.drop_dir && (
                          <div style={{ color: '#6a7f96', fontSize: 13, wordBreak: 'break-all' }}>
                            本地投放目录：{dropInfo.drop_dir}
                          </div>
                        )}

                        {Array.isArray(dropInfo.items) && dropInfo.items.length > 0 && (
                          <div
                            style={{
                              border: '1px solid #d7e3f0',
                              borderRadius: 12,
                              background: '#fff',
                              padding: 12,
                            }}
                          >
                            <div style={{ fontWeight: 900, color: '#12385f', marginBottom: 8 }}>
                              待投放 C++ 模块 zip：{dropInfo.items.length} 个
                            </div>
                            <div style={{ display: 'grid', gap: 8 }}>
                              {dropInfo.items.map((item) => (
                                <div
                                  key={item.name}
                                  style={{
                                    display: 'grid',
                                    gridTemplateColumns: 'minmax(0,1fr) auto',
                                    gap: 10,
                                    alignItems: 'center',
                                    borderTop: '1px solid #edf2f7',
                                    paddingTop: 8,
                                  }}
                                >
                                  <div style={{ minWidth: 0 }}>
                                    <div style={{ fontWeight: 800, color: '#173353', wordBreak: 'break-all' }}>{item.name}</div>
                                    <div style={{ fontSize: 12, color: '#6a7f96', wordBreak: 'break-all' }}>{item.path}</div>
                                  </div>
                                  <button style={styles.whiteBtn} onClick={() => installFromDrop(item.name)}>
                                    安装这个 zip
                                  </button>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                {moduleMgmtAction === 'installed_modules' && (
                  <>
                    <div style={{ ...styles.card, padding: 18 }}>
                      <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 12 }}>
                        已安装模块
                      </div>
                      {renderInstalledModulesTree()}
                    </div>

                    <div style={{ ...styles.card, padding: 16 }}>
                      <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 12 }}>
                        {editingModuleId ? `编辑模块：${editingModuleId}` : '手工新增 / 更新模块'}
                      </div>

                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                        <select
                          value={moduleForm.tool_type}
                          onChange={(e) => setModuleForm({ ...moduleForm, tool_type: e.target.value })}
                          style={styles.input}
                        >
                          {renderToolbarOptions()}
                        </select>
                        <input
                          placeholder="ID"
                          value={moduleForm.id}
                          onChange={(e) => setModuleForm({ ...moduleForm, id: e.target.value })}
                          style={styles.input}
                        />
                        <input placeholder="名称" value={moduleForm.name} onChange={(e) => setModuleForm({ ...moduleForm, name: e.target.value })} style={styles.input} />
                        <input placeholder="可执行文件" value={moduleForm.executable} onChange={(e) => setModuleForm({ ...moduleForm, executable: e.target.value })} style={styles.input} />
                        <input placeholder="工作目录" value={moduleForm.working_dir} onChange={(e) => setModuleForm({ ...moduleForm, working_dir: e.target.value })} style={styles.input} />
                        <input placeholder="标签，英文逗号分隔" value={moduleForm.tags_text} onChange={(e) => setModuleForm({ ...moduleForm, tags_text: e.target.value })} style={styles.input} />
                        <textarea placeholder="描述" value={moduleForm.description} onChange={(e) => setModuleForm({ ...moduleForm, description: e.target.value })} style={{ ...styles.textarea, gridColumn: '1 / span 2', minHeight: 80 }} />
                        <textarea placeholder="命令模板(JSON数组)" value={moduleForm.command_template_text} onChange={(e) => setModuleForm({ ...moduleForm, command_template_text: e.target.value })} style={{ ...styles.textarea, gridColumn: '1 / span 2' }} />
                        <textarea placeholder="输入字段(JSON数组)：包含输入/输出路径、是否用户可见、管理员预填 resources 等" value={moduleForm.inputs_text} onChange={(e) => setModuleForm({ ...moduleForm, inputs_text: e.target.value })} style={{ ...styles.textarea, gridColumn: '1 / span 2', minHeight: 180 }} />
                        <textarea placeholder="并行配置(JSON对象)，保存在 module.json 的 parallel 字段" value={moduleForm.parallel_json_text} onChange={(e) => setModuleForm({ ...moduleForm, parallel_json_text: e.target.value })} style={{ ...styles.textarea, gridColumn: '1 / span 2', minHeight: 110 }} />
                      </div>

                      <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
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
                  </>
                )}

                {moduleMgmtAction === 'toolbars' && (
                  <div style={{ ...styles.card, padding: 18 }}>
                    <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 12 }}>
                      工具栏列表
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 8, marginBottom: 10 }}>
                      <input
                        placeholder="新增工具栏名称"
                        value={newToolbarForm.label}
                        onChange={(e) => setNewToolbarForm({ ...newToolbarForm, label: e.target.value })}
                        style={{ ...styles.input, minHeight: 38, fontSize: 13 }}
                      />
                      <input
                        placeholder="标识，可选"
                        value={newToolbarForm.key}
                        onChange={(e) => setNewToolbarForm({ ...newToolbarForm, key: e.target.value })}
                        style={{ ...styles.input, minHeight: 38, fontSize: 13 }}
                      />
                      <button style={{ ...styles.blueBtn, padding: '8px 12px', fontSize: 13 }} onClick={handleAddToolbar}>
                        添加
                      </button>
                    </div>
                    {renderToolbarAdminList()}
                  </div>
                )}
              </div>
            </div>
          </section>
        )}
        {activeTab === 'user_mgmt' && isAdmin && (
          <section style={{ ...styles.card, padding: 16, minHeight: 'calc(100vh - 98px)' }}>
            <div style={{ fontSize: 22, fontWeight: 900, color: '#12385f', marginBottom: 14 }}>
              用户管理
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 180px', gap: 12 }}>
              <input placeholder="用户名" value={newUserForm.username} onChange={(e) => setNewUserForm({ ...newUserForm, username: e.target.value })} style={styles.input} />
              <input placeholder="密码" type="password" value={newUserForm.password} onChange={(e) => setNewUserForm({ ...newUserForm, password: e.target.value })} style={styles.input} />
              <select value={newUserForm.role} onChange={(e) => setNewUserForm({ ...newUserForm, role: e.target.value })} style={styles.input}>
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
              <input placeholder="安全问题" value={newUserForm.security_question} onChange={(e) => setNewUserForm({ ...newUserForm, security_question: e.target.value })} style={{ ...styles.input, gridColumn: '1 / span 2' }} />
              <input placeholder="安全答案" value={newUserForm.security_answer} onChange={(e) => setNewUserForm({ ...newUserForm, security_answer: e.target.value })} style={styles.input} />
            </div>

            <div style={{ marginTop: 12 }}>
              <button style={styles.blueBtn} onClick={handleAddUser}>新增用户</button>
            </div>

            <div style={{ overflowX: 'auto', marginTop: 16 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff' }}>
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
                        <select value={u.role} onChange={(e) => handleRoleChange(u.username, e.target.value)} style={styles.input}>
                          <option value="user">user</option>
                          <option value="admin">admin</option>
                        </select>
                      </td>
                      <td style={tdStyle}>
                        <select value={u.enabled ? 'enabled' : 'disabled'} onChange={(e) => handleEnabledChange(u.username, e.target.value === 'enabled')} style={styles.input}>
                          <option value="enabled">enabled</option>
                          <option value="disabled">disabled</option>
                        </select>
                      </td>
                      <td style={tdStyle}>
                        <div style={{ display: 'flex', gap: 8 }}>
                          <button style={styles.whiteBtn} onClick={() => handleAdminResetPassword(u.username)}>重置密码</button>
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
        {activeTab === 'data_mgmt' && renderDataManagementPage()}
        {activeTab === 'tasks' && renderTaskManagementPage()}
      </div>

      {windows.filter((w) => !w.minimized).map((w) => (
        <TaskWindow
          key={w.id}
          win={w}
          onMin={(id) => {
            setWindows((prev) => prev.map((x) => (x.id === id ? { ...x, minimized: true } : x)));
            setTaskTrayMinimized(false);
          }}
          onClose={(id) => setWindows((prev) => prev.filter((x) => x.id !== id))}
          onFront={bringFront}
          onMove={moveWindow}
          onStop={stopTaskWindow}
        />
      ))}

      
      {windows.some((w) => w.minimized) && (
          <TaskTrayFloatingWindow
            count={windows.filter((w) => w.minimized).length}
            minimized={taskTrayMinimized}
            onToggleMinimize={() => setTaskTrayMinimized((prev) => !prev)}
          >
            {renderTaskTrayPanel()}
          </TaskTrayFloatingWindow>
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

                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px 170px 160px', gap: 10, marginTop: 10 }}>
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
                    <select
                      value={field.io_role || 'auto'}
                      onChange={(e) => updateInputEditorField(index, { io_role: e.target.value })}
                      style={styles.input}
                      title="用于数据管理：只有 output 字段的结果会登记到数据管理"
                    >
                      <option value="auto">自动判断输入/输出</option>
                      <option value="input">输入文件/资源</option>
                      <option value="output">输出文件/目录</option>
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
          title="C++ 本地模块目录投放说明"
          onClose={() => setShowDropHint(false)}
          width="min(820px, 96vw)"
        >
          <div style={{ lineHeight: 1.9, color: '#173353' }}>
            <p>这里用于 C++ / 本地原生可执行模块投放。zip 内部需要包含 module.json、编译好的 exe、固定资源 resources，以及可选的运行时依赖 deps。C++ 模块不需要上传源码。</p>
            <p>
              当前后端会自动创建并扫描本地投放目录：
              <code>{dropInfo.drop_dir || '项目根目录/module_drop'}</code>
            </p>
            <ol>
              <li>管理员先在“模块所属工具栏”里选择云反演、气溶胶反演或自定义工具类型。</li>
              <li>把 C++ 可执行模块 zip 直接放进这个目录，不需要在网页里选择文件。</li>
              <li>点击“扫描本地目录安装”，后端会先校验 module.json 和缺失文件，再安装通过的 zip。</li>
              <li>系统会尝试从 exe 导入表识别 DLL，并把可找到的非系统 DLL 复制到 deps/auto。</li>
            </ol>
            <p>注意：deps 是运行时依赖目录，只放 exe 运行时缺少的 DLL。源码、头文件、静态库、CMake/vcpkg 配置都不需要上传。</p>
          </div>
        </SimpleOverlay>
      )}
    </div>
  );
}

const thStyle = {
  textAlign: 'left',
  padding: '9px 10px',
  color: '#49627f',
  fontSize: 12,
  fontWeight: 700,
  lineHeight: 1.35,
  borderBottom: '1px solid #dfe8f2',
  background: '#f3f7fb',
  whiteSpace: 'nowrap',
};

const tdStyle = {
  padding: '9px 10px',
  borderBottom: '1px solid #edf2f7',
  color: '#233b56',
  fontSize: 12,
  lineHeight: 1.45,
  verticalAlign: 'middle',
};

const tdEllipsisStyle = {
  ...tdStyle,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const tableActionBtnStyle = {
  ...styles.whiteBtn,
  padding: '6px 10px',
  fontSize: 12,
  borderRadius: 8,
  minWidth: 0,
  whiteSpace: 'nowrap',
};

const tableDangerBtnStyle = {
  ...styles.redBtn,
  padding: '6px 10px',
  fontSize: 12,
  borderRadius: 8,
  minWidth: 0,
  whiteSpace: 'nowrap',
};
const taskThStyle = {
  ...thStyle,
  padding: '12px 12px',
  fontSize: 14,
  fontWeight: 800,
  color: '#24486d',
};

const taskTdStyle = {
  ...tdStyle,
  padding: '12px 12px',
  fontSize: 14,
  lineHeight: 1.55,
  color: '#16385c',
};

const taskTdEllipsisStyle = {
  ...taskTdStyle,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const taskTableActionBtnStyle = {
  ...tableActionBtnStyle,
  padding: '7px 12px',
  fontSize: 13,
  borderRadius: 8,
};

const taskTableDangerBtnStyle = {
  ...tableDangerBtnStyle,
  padding: '7px 12px',
  fontSize: 13,
  borderRadius: 8,
};
