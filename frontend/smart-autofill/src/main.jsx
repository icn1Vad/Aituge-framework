import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ClipboardList,
  Check,
  AlertTriangle,
  Bot,
  BookOpen,
  Eye,
  FilePlus2,
  FileSearch,
  Info,
  MessageCircle,
  RefreshCw,
  Save,
  Send,
  Server,
  Settings2,
  UploadCloud,
  Wand2,
  X,
} from 'lucide-react';
import './styles.css';

const API_BASE = 'http://127.0.0.1:8000';

function formatTime(value) {
  if (!value) return '-';
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(new Date(value));
}

function formatFileSize(value) {
  if (!value) return '-';
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

function parseJsonRows(value) {
  try {
    const rows = JSON.parse(value || '[]');
    return Array.isArray(rows) ? rows : [];
  } catch (error) {
    return [];
  }
}

function parseConfidenceReasons(value) {
  try {
    const parsed = JSON.parse(value || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
}

function confidenceScoreClass(score) {
  const value = Number(score ?? 0);
  if (value >= 90) return 'excellent';
  if (value >= 75) return 'good';
  if (value >= 60) return 'fair';
  if (value >= 40) return 'weak';
  return 'poor';
}

function ConfidenceBadge({ score }) {
  const value = Number(score ?? 0);
  return <span className={`confidence-score ${confidenceScoreClass(value)}`}>{value}分</span>;
}

const FINANCIAL_FORECAST_CATEGORIES = ['营业收入', '合并净利润', '归母净利润', '税后利润', '营业成本', '利润总额'];
const INLINE_EDITABLE_TABLE_IDS = new Set(['non_financial_indicator_table', 'risk_table']);
const MANUAL_TABLE_SCHEMAS = {
  industry_market_analysis_table: {
    title: '行业市场分析',
    itemName: '报告',
    attachmentField: '行业市场分析报告附件',
    fields: [
      { name: '报告日期', label: '报告日期', type: 'date', required: true },
      { name: '报告版本号', label: '报告版本号', type: 'text', required: true, placeholder: '例如：V1.0' },
      { name: '报告上传时间', label: '报告上传时间', type: 'datetime-local', defaultNow: true },
      { name: '行业市场分析报告附件', label: '行业市场分析报告附件', type: 'file', wide: true },
      { name: '报告修改概述', label: '报告修改概述', type: 'textarea', wide: true, placeholder: '请输入报告修改概述' },
    ],
  },
  target_company_asset_valuation_table: {
    title: '标的公司资产评估',
    itemName: '资产评估',
    attachmentField: '上传资产评估报告',
    fields: [
      { name: '评估对象', label: '评估对象', type: 'text', required: true, placeholder: '请输入评估对象' },
      { name: '币种', label: '币种', type: 'select', required: true, options: ['人民币', '美元', '欧元', '其他'], defaultValue: '人民币' },
      { name: '资产评估价值', label: '资产评估价值', type: 'number', placeholder: '单位：万' },
      { name: '交易对价', label: '交易对价', type: 'number', placeholder: '单位：万' },
      { name: '上传资产评估报告', label: '上传资产评估报告', type: 'file', wide: true },
      { name: '评估机构', label: '评估机构', type: 'text', wide: true, placeholder: '请输入评估机构' },
    ],
  },
};
const FINANCIAL_FORECAST_COLUMN_WIDTHS = {
  序号: 48,
  类别: 150,
  操作: 76,
  default: 150,
};

function financialForecastColumnLabel(column) {
  const labelMap = {
    '投资年份（t0）': '投资年份（t0）',
    't0+1': '第一年',
    't0+2': '第二年',
    't0+3': '第三年',
    't0+4': '第四年',
    't0+5': '第五年',
    't0+6': '第六年',
    't0+7': '第七年',
  };
  return labelMap[column] || column;
}

function manualTableSchema(field) {
  return MANUAL_TABLE_SCHEMAS[field.field_id] || {
    title: field.field_name,
    itemName: '记录',
    fields: (field.columns || [])
      .filter((column) => !['序号', '操作'].includes(column))
      .map((column) => ({ name: column, label: column, type: 'text' })),
  };
}

function App() {
  const [health, setHealth] = useState({ state: 'checking', text: '检查中' });
  const [tasks, setTasks] = useState([]);
  const [taskName, setTaskName] = useState('');
  const [sections, setSections] = useState([]);
  const [activeTask, setActiveTask] = useState(null);
  const [formConfig, setFormConfig] = useState({ groups: [] });
  const [formValues, setFormValues] = useState({});
  const [fieldResults, setFieldResults] = useState({});
  const [dirtyFields, setDirtyFields] = useState({});
  const [selectedManualRows, setSelectedManualRows] = useState({});
  const [manualTableModal, setManualTableModal] = useState(null);
  const [activeSource, setActiveSource] = useState(null);
  const [sourcePreview, setSourcePreview] = useState(null);
  const [exceptions, setExceptions] = useState([]);
  const [showExceptions, setShowExceptions] = useState(false);
  const [rulesCount, setRulesCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [fillingTaskId, setFillingTaskId] = useState(null);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [knowledgeDocs, setKnowledgeDocs] = useState([]);
  const [knowledgeUploading, setKnowledgeUploading] = useState(false);
  const [chatSessionId, setChatSessionId] = useState(null);
  const [chatQuestion, setChatQuestion] = useState('');
  const [chatMessages, setChatMessages] = useState([]);
  const [chatSources, setChatSources] = useState([]);
  const [chatLoading, setChatLoading] = useState(false);
  const [message, setMessage] = useState('');

  const stats = useMemo(() => {
    return {
      total: tasks.length,
      pendingParse: tasks.filter((task) => ['pending', 'uploaded'].includes(task.parse_status)).length,
      reviewing: tasks.filter((task) => task.review_status === 'reviewing').length,
    };
  }, [tasks]);

  async function loadHealth() {
    try {
      const response = await fetch(`${API_BASE}/api/health`);
      const data = await response.json();
      setHealth({ state: 'ok', text: `${data.service} 已连接` });
    } catch (error) {
      setHealth({ state: 'error', text: '后端未连接' });
    }
  }

  async function loadTasks() {
    const response = await fetch(`${API_BASE}/api/tasks`);
    const data = await response.json();
    setTasks(data.items ?? []);
  }

  async function loadFormConfig() {
    const [configResponse, rulesResponse] = await Promise.all([
      fetch(`${API_BASE}/api/form-config`),
      fetch(`${API_BASE}/api/field-rules`),
    ]);
    const config = await configResponse.json();
    const rules = await rulesResponse.json();
    setFormConfig(config);
    setRulesCount((rules.rules ?? []).length);
  }

  async function loadKnowledgeDocs() {
    const response = await fetch(`${API_BASE}/api/knowledge/documents`);
    const data = await response.json();
    setKnowledgeDocs(data.items ?? []);
  }

  async function refresh() {
    setLoading(true);
    setMessage('');
    try {
      await loadHealth();
      await loadTasks();
      await loadFormConfig();
      await loadKnowledgeDocs();
    } catch (error) {
      setMessage('任务列表加载失败，请确认后端服务已启动。');
    } finally {
      setLoading(false);
    }
  }

  async function createTask(event) {
    event.preventDefault();
    const name = taskName.trim();
    if (!name) {
      setMessage('请输入任务名称。');
      return;
    }

    setLoading(true);
    setMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_name: name }),
      });
      if (!response.ok) throw new Error('create failed');
      setTaskName('');
      setMessage('任务已创建。');
      await loadTasks();
    } catch (error) {
      setMessage('任务创建失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  }

  async function uploadReport(taskId, file) {
    if (!file) return;
    const fileName = file.name.toLowerCase();
    if (!fileName.endsWith('.doc') && !fileName.endsWith('.docx')) {
      setMessage('仅支持上传 .doc 或 .docx 文件。');
      return;
    }

    const formData = new FormData();
    formData.append('file', file);

    setLoading(true);
    setMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${taskId}/upload`, {
        method: 'POST',
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'upload failed');
      }
      setMessage(`已上传：${data.report_file_name}`);
      if (activeTask?.task_id === taskId) {
        setSections([]);
        setActiveTask(data);
      }
      await loadTasks();
    } catch (error) {
      setMessage(error.message || '文件上传失败，请检查文件后重试。');
    } finally {
      setLoading(false);
    }
  }

  async function uploadKnowledgeDocument(file) {
    if (!file) return;
    const fileName = file.name.toLowerCase();
    if (!fileName.endsWith('.doc') && !fileName.endsWith('.docx')) {
      setMessage('知识库仅支持上传 .doc 或 .docx 文件。');
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    setKnowledgeUploading(true);
    setMessage('正在构建知识库...');
    try {
      const response = await fetch(`${API_BASE}/api/knowledge/documents`, {
        method: 'POST',
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'knowledge upload failed');
      setMessage(`知识库已更新：${data.file_name}`);
      await loadKnowledgeDocs();
    } catch (error) {
      setMessage(error.message || '知识库文档上传失败。');
    } finally {
      setKnowledgeUploading(false);
    }
  }

  async function askKnowledge(event) {
    event.preventDefault();
    const question = chatQuestion.trim();
    if (!question || chatLoading) return;
    setChatQuestion('');
    setChatMessages((current) => [...current, { role: 'user', content: question }]);
    setChatLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/knowledge/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: chatSessionId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'chat failed');
      setChatSessionId(data.session_id);
      setChatSources(data.sources ?? []);
      setChatMessages((current) => [...current, { role: 'assistant', content: data.answer }]);
    } catch (error) {
      setChatMessages((current) => [...current, { role: 'assistant', content: error.message || '问答失败，请稍后重试。' }]);
    } finally {
      setChatLoading(false);
    }
  }

  async function parseTask(task) {
    if (!task.report_file_name) {
      setMessage('请先上传 .docx 文件。');
      return;
    }

    setLoading(true);
    setMessage('正在解析 Word 文档...');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${task.task_id}/parse`, {
        method: 'POST',
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'parse failed');
      }
      if (data.parse_status === 'failed') {
        throw new Error(data.parse_error || '文档解析失败');
      }
      setMessage(`解析完成：${data.section_count ?? 0} 个切片，${data.table_count ?? 0} 个表格。`);
      setActiveTask(data);
      await loadTasks();
      await loadSections(data);
    } catch (error) {
      setMessage(error.message || '文档解析失败，请检查文件后重试。');
      await loadTasks();
    } finally {
      setLoading(false);
    }
  }

  async function loadSections(task) {
    setLoading(true);
    setMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${task.task_id}/sections`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'sections failed');
      }
      setActiveTask(task);
      setSections(data.items ?? []);
      if ((data.items ?? []).length === 0) {
        setMessage('当前任务还没有解析结果。');
      }
    } catch (error) {
      setMessage(error.message || '章节列表加载失败。');
    } finally {
      setLoading(false);
    }
  }

  function applyFieldResults(items) {
    const values = {};
    const resultMap = {};
    for (const item of items) {
      values[item.field_id] = item.field_value;
      resultMap[item.field_id] = item;
    }
    setFormValues(values);
    setFieldResults(resultMap);
    setDirtyFields({});
  }

  async function fillTask(task) {
    if (task.parse_status !== 'success') {
      setMessage('请先解析 Word 文档，再执行自动填单。');
      return;
    }

    setFillingTaskId(task.task_id);
    setMessage('正在使用大模型自动填单，请稍候...');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${task.task_id}/fill`, {
        method: 'POST',
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'fill failed');
      }
      applyFieldResults(data.items ?? []);
      setActiveTask(task);
      setMessage(`自动填单完成：${data.items?.length ?? 0} 个字段已生成结果。`);
      await loadTasks();
    } catch (error) {
      setMessage(error.message || '自动填单失败，请检查解析结果。');
    } finally {
      setFillingTaskId(null);
    }
  }

  async function loadResults(task) {
    setLoading(true);
    setMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${task.task_id}/results`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'results failed');
      }
      applyFieldResults(data.items ?? []);
      setActiveTask(task);
      if ((data.items ?? []).length === 0) {
        setMessage('当前任务还没有自动填单结果。');
      }
    } catch (error) {
      setMessage(error.message || '填单结果加载失败。');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  function updateFieldValue(fieldId, value) {
    setFormValues((current) => ({ ...current, [fieldId]: value }));
    setDirtyFields((current) => ({ ...current, [fieldId]: true }));
  }

  function getManualTableRows(field) {
    return parseJsonRows(formValues[field.field_id] ?? '');
  }

  function saveManualTableRows(field, rows) {
    const normalizedRows = rows.map((row, index) => ({
      ...row,
      序号: String(index + 1),
    }));
    updateFieldValue(field.field_id, JSON.stringify(normalizedRows));
  }

  function saveTableRows(field, rows) {
    updateFieldValue(field.field_id, JSON.stringify(rows));
  }

  function addFinancialForecastRow(field) {
    const columns = field.columns ?? [];
    const rows = parseJsonRows(formValues[field.field_id] ?? '');
    const usedCategories = new Set(rows.map((row) => row.类别).filter(Boolean));
    const category = FINANCIAL_FORECAST_CATEGORIES.find((item) => !usedCategories.has(item)) || FINANCIAL_FORECAST_CATEGORIES[0];
    const nextRow = Object.fromEntries(columns.map((column) => [column, '']));
    nextRow.类别 = category;
    saveTableRows(field, [...rows, nextRow]);
  }

  function updateFinancialForecastCell(field, rowIndex, column, value) {
    const rows = parseJsonRows(formValues[field.field_id] ?? '');
    const nextRows = rows.map((row, index) => (
      index === rowIndex ? { ...row, [column]: value } : row
    ));
    saveTableRows(field, nextRows);
  }

  function deleteFinancialForecastRow(field, rowIndex) {
    const rows = parseJsonRows(formValues[field.field_id] ?? '');
    saveTableRows(field, rows.filter((_, index) => index !== rowIndex));
  }

  function addInlineTableRow(field) {
    const columns = field.columns ?? [];
    const nextRow = Object.fromEntries(columns.map((column) => [column, '']));
    nextRow.序号 = String(parseJsonRows(formValues[field.field_id] ?? '').length + 1);
    saveTableRows(field, [...parseJsonRows(formValues[field.field_id] ?? ''), nextRow]);
  }

  function updateInlineTableCell(field, rowIndex, column, value) {
    const rows = parseJsonRows(formValues[field.field_id] ?? '');
    const nextRows = rows.map((row, index) => (
      index === rowIndex ? { ...row, [column]: value } : row
    ));
    saveTableRows(field, nextRows);
  }

  function deleteInlineTableRow(field, rowIndex) {
    const rows = parseJsonRows(formValues[field.field_id] ?? '');
    const nextRows = rows
      .filter((_, index) => index !== rowIndex)
      .map((row, index) => ({ ...row, 序号: String(index + 1) }));
    saveTableRows(field, nextRows);
  }

  function nowDateTimeInputValue() {
    const now = new Date();
    const offset = now.getTimezoneOffset() * 60000;
    return new Date(now.getTime() - offset).toISOString().slice(0, 16);
  }

  function createManualModalForm(field, initialRow = {}) {
    const schema = manualTableSchema(field);
    const form = { 附件存储路径: initialRow.附件存储路径 ?? '' };
    for (const item of schema.fields) {
      if (initialRow[item.name] !== undefined) {
        form[item.name] = initialRow[item.name];
      } else if (item.defaultNow) {
        form[item.name] = nowDateTimeInputValue();
      } else if (item.defaultValue !== undefined) {
        form[item.name] = item.defaultValue;
      } else {
        form[item.name] = '';
      }
    }
    return form;
  }

  function updateManualModalForm(fieldName, value) {
    setManualTableModal((current) => (
      current
        ? { ...current, form: { ...current.form, [fieldName]: value } }
        : current
    ));
  }

  async function uploadManualAttachment(file, attachmentFieldName) {
    if (!activeTask) {
      setMessage('请先选择一个任务。');
      return;
    }
    const payload = new FormData();
    payload.append('file', file);
    setMessage('正在上传附件...');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${activeTask.task_id}/manual-attachments`, {
        method: 'POST',
        body: payload,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || '附件上传失败');
      }
      setManualTableModal((current) => (
        current
          ? {
              ...current,
              form: {
                ...current.form,
                [attachmentFieldName]: data.file_name || file.name,
                附件存储路径: data.stored_path || '',
                ...(current.form.报告上传时间 !== undefined ? { 报告上传时间: nowDateTimeInputValue() } : {}),
              },
            }
          : current
      ));
      setMessage('附件已上传。');
    } catch (error) {
      setMessage(error.message || '附件上传失败，请重试。');
    }
  }

  async function uploadInlineTableAttachment(field, rowIndex, column, file) {
    if (!activeTask) {
      setMessage('请先选择一个任务。');
      return;
    }
    const payload = new FormData();
    payload.append('file', file);
    setMessage('正在上传附件...');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${activeTask.task_id}/manual-attachments`, {
        method: 'POST',
        body: payload,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || '附件上传失败');
      }
      const rows = parseJsonRows(formValues[field.field_id] ?? '');
      const nextRows = rows.map((row, index) => (
        index === rowIndex
          ? {
              ...row,
              [column]: data.file_name || file.name,
              [`${column}存储路径`]: data.stored_path || '',
            }
          : row
      ));
      saveTableRows(field, nextRows);
      setMessage('附件已上传。');
    } catch (error) {
      setMessage(error.message || '附件上传失败，请重试。');
    }
  }

  function selectedManualRowIndex(field) {
    const index = selectedManualRows[field.field_id];
    return Number.isInteger(index) ? index : -1;
  }

  function addManualTableRow(field) {
    setManualTableModal({
      mode: 'add',
      field,
      index: -1,
      form: createManualModalForm(field),
    });
  }

  function editManualTableRow(field) {
    const rows = getManualTableRows(field);
    const index = selectedManualRowIndex(field);
    if (index < 0 || index >= rows.length) {
      setMessage(`请先选择一条${manualTableSchema(field).itemName}记录。`);
      return;
    }
    setManualTableModal({
      mode: 'edit',
      field,
      index,
      form: createManualModalForm(field, rows[index]),
    });
  }

  function deleteManualTableRow(field) {
    const rows = getManualTableRows(field);
    const index = selectedManualRowIndex(field);
    if (index < 0 || index >= rows.length) {
      setMessage(`请先选择一条${manualTableSchema(field).itemName}记录。`);
      return;
    }
    if (!window.confirm(`确认删除选中的${manualTableSchema(field).itemName}记录？`)) return;
    const nextRows = rows.filter((_, rowIndex) => rowIndex !== index);
    saveManualTableRows(field, nextRows);
    setSelectedManualRows((current) => ({ ...current, [field.field_id]: -1 }));
  }

  function viewManualTableRow(field) {
    const rows = getManualTableRows(field);
    const index = selectedManualRowIndex(field);
    if (index < 0 || index >= rows.length) {
      setMessage(`请先选择一条${manualTableSchema(field).itemName}记录。`);
      return;
    }
    setManualTableModal({
      mode: 'view',
      field,
      index,
      form: createManualModalForm(field, rows[index]),
    });
  }

  function submitManualTableModal(event) {
    event.preventDefault();
    if (!manualTableModal || manualTableModal.mode === 'view') {
      setManualTableModal(null);
      return;
    }
    const { field, index, form, mode } = manualTableModal;
    const schema = manualTableSchema(field);
    const rows = getManualTableRows(field);
    for (const item of schema.fields) {
      if (item.required && !form[item.name]) {
        setMessage(`请填写${item.label}。`);
        return;
      }
    }
    const nextRow = { 附件存储路径: form.附件存储路径 };
    for (const item of schema.fields) {
      nextRow[item.name] = form[item.name] ?? '';
    }
    if (mode === 'add') {
      saveManualTableRows(field, [...rows, nextRow]);
    } else if (index >= 0 && index < rows.length) {
      saveManualTableRows(field, rows.map((row, rowIndex) => (rowIndex === index ? { ...row, ...nextRow } : row)));
    }
    setManualTableModal(null);
  }

  async function saveResults() {
    if (!activeTask) {
      setMessage('请先选择一个任务。');
      return;
    }

    setLoading(true);
    setMessage('正在保存审核修改...');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${activeTask.task_id}/results`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: Object.entries(formValues).map(([field_id, field_value]) => ({
            field_id,
            field_value: String(field_value ?? ''),
          })),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'save failed');
      }
      applyFieldResults(data.items ?? []);
      setMessage('审核修改已保存。');
      await loadTasks();
    } catch (error) {
      setMessage(error.message || '保存失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  }

  async function confirmField(fieldId) {
    if (!activeTask) {
      setMessage('请先选择一个任务。');
      return;
    }
    if (dirtyFields[fieldId]) {
      setMessage('该字段有未保存修改，请先保存后再确认。');
      return;
    }

    setLoading(true);
    setMessage('');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${activeTask.task_id}/results/${fieldId}/confirm`, {
        method: 'POST',
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'confirm failed');
      }
      setFieldResults((current) => ({ ...current, [fieldId]: data }));
      setMessage(`已确认字段：${data.field_name}`);
    } catch (error) {
      setMessage(error.message || '字段确认失败。');
    } finally {
      setLoading(false);
    }
  }

  async function validateTask(task = activeTask) {
    if (!task) {
      setMessage('请先选择一个任务。');
      return;
    }

    setLoading(true);
    setMessage('正在校验异常项...');
    try {
      const response = await fetch(`${API_BASE}/api/tasks/${task.task_id}/validate`, {
        method: 'POST',
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'validate failed');
      }
      setExceptions(data.items ?? []);
      setShowExceptions(true);
      setActiveTask(task);
      setMessage(`校验完成：发现 ${data.items?.length ?? 0} 个异常项。`);
    } catch (error) {
      setMessage(error.message || '异常校验失败。');
    } finally {
      setLoading(false);
    }
  }

  function scrollToGroup(groupId) {
    document.getElementById(`form-group-${groupId}`)?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
  }

  async function ensureSectionsLoaded() {
    if (!activeTask) return [];
    if (sections.length > 0) return sections;
    const response = await fetch(`${API_BASE}/api/tasks/${activeTask.task_id}/sections`);
    const data = await response.json();
    const items = data.items ?? [];
    setSections(items);
    return items;
  }

  function normalizePreviewText(value) {
    return String(value || '').replace(/\s+/g, ' ').replace(/…$/, '').trim();
  }

  function sourceHighlightNeedles(sourceText) {
    const source = String(sourceText || '');
    const lines = source
      .split(/\n+/)
      .map((line) => normalizePreviewText(line))
      .filter((line) => line.length >= 2);
    if (lines.length > 1) {
      return lines
        .flatMap((line) => {
          const cells = line.split('|').map((cell) => normalizePreviewText(cell)).filter((cell) => cell.length >= 2);
          return [line, ...cells];
        })
        .filter((item, index, array) => array.indexOf(item) === index)
        .sort((a, b) => b.length - a.length);
    }
    const cleanSource = normalizePreviewText(source);
    if (!cleanSource) return [];
    return [cleanSource, cleanSource.slice(0, Math.min(cleanSource.length, 90))].filter(Boolean);
  }

  function renderHighlightedText(content, sourceText) {
    const fullText = String(content || '');
    const needles = sourceHighlightNeedles(sourceText);
    if (needles.length === 0) return fullText;

    const ranges = [];
    for (const needle of needles) {
      let start = 0;
      while (needle && start < fullText.length) {
        const index = fullText.indexOf(needle, start);
        if (index < 0) break;
        const end = index + needle.length;
        if (!ranges.some((range) => index < range.end && end > range.start)) {
          ranges.push({ start: index, end });
        }
        start = end;
      }
    }

    if (ranges.length === 0) {
      return (
        <>
          <mark>{sourceText}</mark>
          {'\n\n'}
          {fullText}
        </>
      );
    }

    ranges.sort((a, b) => a.start - b.start);
    const nodes = [];
    let cursor = 0;
    ranges.forEach((range, index) => {
      if (range.start > cursor) {
        nodes.push(fullText.slice(cursor, range.start));
      }
      nodes.push(<mark key={`${range.start}-${index}`}>{fullText.slice(range.start, range.end)}</mark>);
      cursor = range.end;
    });
    if (cursor < fullText.length) {
      nodes.push(fullText.slice(cursor));
    }

    return <>{nodes}</>;
  }

  function renderHighlightedTextLegacy(content, sourceText) {
    const fullText = String(content || '');
    const cleanSource = normalizePreviewText(sourceText);
    if (!cleanSource) return fullText;

    return (
      <>
        {fullText}
      </>
    );
  }

  async function openSourcePreview(sourceResult) {
    if (!sourceResult?.result_id && !sourceResult?.source_text) return;
    setLoading(true);
    try {
      let previewResult = sourceResult;
      const latestResultsResponse = await fetch(`${API_BASE}/api/tasks/${activeTask.task_id}/results`);
      if (latestResultsResponse.ok) {
        const latestResultsData = await latestResultsResponse.json();
        const latestItems = latestResultsData.items ?? [];
        const latestResult = latestItems.find((item) => item.field_id === sourceResult.field_id);
        if (latestResult) {
          previewResult = latestResult;
          setFieldResults(Object.fromEntries(latestItems.map((item) => [item.field_id, item])));
        }
      }
      const items = await ensureSectionsLoaded();
      const matched = previewResult.source_section
        ? items.find((item) => item.title === previewResult.source_section)
          || items.find((item) => previewResult.source_section.includes(item.title) || item.title.includes(previewResult.source_section))
        : null;
      const previewUrl = `${API_BASE}/api/tasks/${activeTask.task_id}/word-preview?result_id=${encodeURIComponent(previewResult.result_id || '')}`;
      const response = await fetch(previewUrl);
      const html = await response.text();
      if (!response.ok) {
        throw new Error('word preview failed');
      }
      setSourcePreview({
        result: previewResult,
        section: matched ?? null,
        html,
      });
    } catch (error) {
      setMessage('来源章节预览加载失败。');
    } finally {
      setLoading(false);
    }
  }

  function renderFieldControl(field) {
    const value = formValues[field.field_id] ?? '';
    if (field.component_type === 'textarea') {
      return (
        <textarea
          maxLength={field.max_length ?? undefined}
          value={value}
          onChange={(event) => updateFieldValue(field.field_id, event.target.value)}
        />
      );
    }
    if (field.component_type === 'select') {
      return (
        <select value={value} onChange={(event) => updateFieldValue(field.field_id, event.target.value)}>
          <option value="">请选择</option>
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    }
    if (field.component_type === 'radio') {
      return (
        <div className="radio-group">
          {(field.options ?? []).map((option) => (
            <label key={option}>
              <input
                checked={value === option}
                name={field.field_id}
                type="radio"
                value={option}
                onChange={(event) => updateFieldValue(field.field_id, event.target.value)}
              />
              {option}
            </label>
          ))}
        </div>
      );
    }
    if (field.component_type === 'manual_table') {
      const rows = parseJsonRows(value);
      const columns = field.columns ?? [];
      const selectedIndex = selectedManualRowIndex(field);
      const manualColumnWidths = columns.map((column) => {
        if (column === '序号') return 64;
        if (column === '币种') return 110;
        if (column === '操作') return 76;
        if (column.includes('附件') || column.includes('报告')) return 220;
        if (column.includes('价值') || column.includes('对价')) return 140;
        return 180;
      });
      const minWidth = 44 + manualColumnWidths.reduce((total, width) => total + width, 0);
      const tableStyle = {
        gridTemplateColumns: `44px ${manualColumnWidths.map((width) => `${width}px`).join(' ')}`,
        width: `${minWidth}px`,
        minWidth: `${minWidth}px`,
      };
      return (
        <div className="manual-table-panel">
          <div className="manual-table-toolbar">
            <button type="button" className="primary" onClick={() => addManualTableRow(field)}>新增</button>
            <button type="button" onClick={() => editManualTableRow(field)}>修改</button>
            <button type="button" className="danger" onClick={() => deleteManualTableRow(field)}>删除</button>
            <button type="button" onClick={() => viewManualTableRow(field)}>查看</button>
          </div>
          <div className="field-table-wrap manual-table">
            <div className="field-table" style={tableStyle}>
              <span>
                <input type="checkbox" disabled />
              </span>
              {columns.map((column) => (
                <span key={column}>{column}</span>
              ))}
            </div>
            {rows.length > 0 ? (
              <div className="field-table-body">
                {rows.map((row, index) => (
                  <div className="field-table-row" key={`${field.field_id}-${index}`} style={tableStyle}>
                    <span>
                      <input
                        type="checkbox"
                        checked={selectedIndex === index}
                        onChange={() => setSelectedManualRows((current) => ({ ...current, [field.field_id]: index }))}
                      />
                    </span>
                    {columns.map((column) => (
                      <span key={column}>{row[column] ?? ''}</span>
                    ))}
                  </div>
                ))}
              </div>
            ) : (
              <div className="manual-table-empty" style={{ minWidth: `${minWidth}px` }}>暂无数据</div>
            )}
          </div>
        </div>
      );
    }
    if (field.field_id === 'financial_forecast_table') {
      const rows = parseJsonRows(value);
      const columns = field.columns ?? [];
      const valueColumns = columns.filter((column) => column !== '类别');
      const columnWidths = [
        FINANCIAL_FORECAST_COLUMN_WIDTHS.序号,
        FINANCIAL_FORECAST_COLUMN_WIDTHS.类别,
        ...valueColumns.map(() => FINANCIAL_FORECAST_COLUMN_WIDTHS.default),
        FINANCIAL_FORECAST_COLUMN_WIDTHS.操作,
      ];
      const minWidth = columnWidths.reduce((total, width) => total + width, 0);
      const tableStyle = {
        gridTemplateColumns: columnWidths.map((width) => `${width}px`).join(' '),
        width: `${minWidth}px`,
        minWidth: `${minWidth}px`,
      };
      return (
        <div className="forecast-table-panel">
          <div className="forecast-table-toolbar">
            <label>
              <span>币种：</span>
              <select
                value={formValues.financial_forecast_currency || '人民币'}
                onChange={(event) => updateFieldValue('financial_forecast_currency', event.target.value)}
              >
                {['人民币', '美元', '欧元', '其他'].map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </label>
            <span>单位：万</span>
            <button type="button" onClick={() => addFinancialForecastRow(field)}>
              新增
            </button>
          </div>
          <div className="field-table-wrap scrollable forecast-table">
            <div className="field-table" style={tableStyle}>
              <span>序号</span>
              <span>类别</span>
              {valueColumns.map((column) => (
                <span key={column}>{financialForecastColumnLabel(column)}</span>
              ))}
              <span>操作</span>
            </div>
            {rows.length > 0 ? (
              <div className="field-table-body">
                {rows.map((row, index) => (
                  <div className="field-table-row" key={`${field.field_id}-${index}`} style={tableStyle}>
                    <span>{index + 1}</span>
                    <span>
                      <select
                        value={row.类别 ?? ''}
                        onChange={(event) => updateFinancialForecastCell(field, index, '类别', event.target.value)}
                      >
                        <option value="">请选择</option>
                        {FINANCIAL_FORECAST_CATEGORIES.map((category) => (
                          <option key={category} value={category}>{category}</option>
                        ))}
                      </select>
                    </span>
                    {valueColumns.map((column) => (
                      <span key={column}>
                        <input
                          inputMode="decimal"
                          placeholder={`${column}(万)`}
                          value={row[column] ?? ''}
                          onChange={(event) => updateFinancialForecastCell(field, index, column, event.target.value)}
                        />
                      </span>
                    ))}
                    <span>
                      <button type="button" className="link-delete" onClick={() => deleteFinancialForecastRow(field, index)}>
                        删除
                      </button>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="manual-table-empty" style={{ minWidth: `${minWidth}px` }}>暂无数据</div>
            )}
          </div>
        </div>
      );
    }
    if (INLINE_EDITABLE_TABLE_IDS.has(field.field_id)) {
      const rows = parseJsonRows(value);
      const columns = field.columns ?? [];
      const dataColumns = columns.filter((column) => column !== '序号');
      const gridTemplateColumns = field.field_id === 'risk_table'
        ? '46px minmax(160px, 1.1fr) minmax(120px, 0.8fr) minmax(260px, 1.7fr) minmax(220px, 1.5fr) 60px'
        : '46px minmax(180px, 1fr) minmax(300px, 1.7fr) minmax(140px, 0.8fr) minmax(140px, 0.8fr) 60px';
      const tableStyle = {
        gridTemplateColumns,
        width: '100%',
        minWidth: '860px',
      };
      return (
        <div className="inline-edit-table-panel">
          <div className="inline-edit-table-toolbar">
            <button type="button" onClick={() => addInlineTableRow(field)}>新增</button>
          </div>
          <div className="field-table-wrap inline-edit-table">
            <div className="field-table" style={tableStyle}>
              <span>序号</span>
              {dataColumns.map((column) => (
                <span key={column}>{column}</span>
              ))}
              <span>操作</span>
            </div>
            {rows.length > 0 ? (
              <div className="field-table-body">
                {rows.map((row, index) => (
                  <div className="field-table-row" key={`${field.field_id}-${index}`} style={tableStyle}>
                    <span>{index + 1}</span>
                    {dataColumns.map((column) => (
                      <span key={column}>
                        {field.field_id === 'risk_table' && column === '风险评估报告' ? (
                          <label className="inline-file-cell">
                            <input
                              accept=".doc,.docx,.pdf,.xls,.xlsx,.ppt,.pptx"
                              type="file"
                              onChange={(event) => {
                                const file = event.target.files?.[0];
                                if (file) {
                                  uploadInlineTableAttachment(field, index, column, file);
                                }
                              }}
                            />
                            <small>{row[column] || '上传附件'}</small>
                          </label>
                        ) : (
                          <input
                            value={row[column] ?? ''}
                            onChange={(event) => updateInlineTableCell(field, index, column, event.target.value)}
                          />
                        )}
                      </span>
                    ))}
                    <span>
                      <button type="button" className="link-delete" onClick={() => deleteInlineTableRow(field, index)}>
                        删除
                      </button>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="manual-table-empty inline-empty">暂无数据</div>
            )}
          </div>
        </div>
      );
    }
    if (field.component_type === 'table') {
      const rows = parseJsonRows(value);
      const columnCount = Math.max(field.columns?.length ?? 1, 1);
      const isScrollableTable = field.field_id === 'financial_forecast_table';
      const tableStyle = {
        gridTemplateColumns: `repeat(${columnCount}, minmax(${isScrollableTable ? 140 : 120}px, 1fr))`,
        ...(isScrollableTable ? { minWidth: `${columnCount * 140}px` } : {}),
      };
      return (
        <div className={`field-table-wrap ${isScrollableTable ? 'scrollable' : ''}`}>
          <div className="field-table" style={tableStyle}>
            {(field.columns ?? []).map((column) => (
              <span key={column}>{column}</span>
            ))}
          </div>
          {rows.length > 0 ? (
            <div className="field-table-body">
              {rows.map((row, index) => (
                <div className="field-table-row" key={`${field.field_id}-${index}`} style={tableStyle}>
                  {(field.columns ?? []).map((column) => (
                    <span key={column}>{row[column] ?? ''}</span>
                  ))}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      );
    }
    return (
      <input
        value={value}
        onChange={(event) => updateFieldValue(field.field_id, event.target.value)}
      />
    );
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <h1>项目储备管理</h1>
        </div>
        <div className="topbar-actions">
          <button className="ghost" onClick={() => setKnowledgeOpen(true)} type="button">
            <BookOpen size={18} />
            知识库
          </button>
          <div className={`health ${health.state}`}>
            <Server size={18} />
            <span>{health.text}</span>
          </div>
        </div>
      </header>

      <nav className="case-tabs">
        <button className="active">项目信息</button>
        <button>可研编制</button>
        <button>可研评审</button>
      </nav>

      <section className="summary">
        <div>
          <span>任务总数</span>
          <strong>{stats.total}</strong>
        </div>
        <div>
          <span>待上传/解析</span>
          <strong>{stats.pendingParse}</strong>
        </div>
        <div>
          <span>审核中</span>
          <strong>{stats.reviewing}</strong>
        </div>
      </section>

      <section className="workspace">
        <aside className="create-panel">
          <div className="panel-title">
            <FilePlus2 size={20} />
            <h2>新建填单任务</h2>
          </div>
          <form onSubmit={createTask}>
            <label htmlFor="task-name">任务名称</label>
            <input
              id="task-name"
              value={taskName}
              onChange={(event) => setTaskName(event.target.value)}
              placeholder="例如：XX 项目可研报告填单"
              maxLength={100}
            />
            <button disabled={loading} type="submit">
              创建任务
            </button>
          </form>
          {message ? <p className="message">{message}</p> : null}
        </aside>

        <section className="task-panel">
          <div className="task-header">
            <div className="panel-title">
              <ClipboardList size={20} />
              <h2>任务列表</h2>
            </div>
            <button className="ghost" disabled={loading} onClick={refresh}>
              <RefreshCw size={16} />
              刷新
            </button>
          </div>

          <div className="task-table">
            <div className="task-row head">
              <span>任务名称</span>
              <span>报告文件</span>
              <span>解析状态</span>
              <span>填单状态</span>
              <span>审核状态</span>
              <span>创建时间</span>
              <span>操作</span>
            </div>
            {tasks.length === 0 ? (
              <div className="empty">暂无任务，请先创建一个填单任务。</div>
            ) : (
              tasks.map((task) => (
                <div className="task-row" key={task.task_id}>
                  <strong>{task.task_name}</strong>
                  <span>
                    {task.report_file_name ? (
                      <>
                        {task.report_file_name}
                        <small>{formatFileSize(task.report_file_size)}</small>
                      </>
                    ) : (
                      '未上传'
                    )}
                  </span>
                  <span>{task.parse_status}</span>
                  <span>{task.fill_status}</span>
                  <span>{task.review_status}</span>
                  <span>{formatTime(task.created_at)}</span>
                  <div className="row-actions">
                    <label className="upload-action">
                      <UploadCloud size={16} />
                      上传
                      <input
                        accept=".doc,.docx"
                        disabled={loading}
                        type="file"
                        onChange={(event) => {
                          uploadReport(task.task_id, event.target.files?.[0]);
                          event.target.value = '';
                        }}
                      />
                    </label>
                    <button
                      className="icon-action"
                      disabled={loading || !task.report_file_name}
                      onClick={() => parseTask(task)}
                      title="解析 Word"
                    >
                      <FileSearch size={16} />
                    </button>
                    <button
                      className="icon-action"
                      disabled={loading}
                      onClick={() => loadSections(task)}
                      title="查看章节"
                    >
                      <Eye size={16} />
                    </button>
                    <button
                      className="icon-action"
                      disabled={loading || fillingTaskId === task.task_id || task.parse_status !== 'success'}
                      onClick={() => fillTask(task)}
                      title="自动填单"
                    >
                      <Wand2 size={16} />
                    </button>
                    <button
                      className="icon-action text-action"
                      disabled={loading}
                      onClick={() => loadResults(task)}
                      title="查看填单结果"
                    >
                      结果
                    </button>
                    <button
                      className="icon-action text-action"
                      disabled={loading}
                      onClick={() => validateTask(task)}
                      title="异常校验"
                    >
                      异常
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </section>
      </section>

      <section className="section-panel">
        <div className="task-header">
          <div className="panel-title">
            <FileSearch size={20} />
            <h2>文档解析结果</h2>
          </div>
          <span className="section-meta">
            {activeTask ? activeTask.task_name : '未选择任务'}
          </span>
        </div>

        {sections.length === 0 ? (
          <div className="empty">选择已上传的任务并点击解析后，在这里查看章节、正文和表格切片。</div>
        ) : (
          <div className="section-list">
            {sections.map((section) => (
              <article className="section-item" key={section.section_id}>
                <div className="section-title">
                  <strong>{section.title}</strong>
                  <span>{section.content_type}</span>
                  <span>段落 {section.paragraph_index}</span>
                </div>
                <p>{section.content}</p>
                {section.keywords.length > 0 ? (
                  <div className="keywords">
                    {section.keywords.map((keyword) => (
                      <span key={keyword}>{keyword}</span>
                    ))}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="form-config-panel">
        <div className="task-header form-tools">
          <div className="form-actions">
            <span className="section-meta">
              {activeTask ? activeTask.task_name : '未选择任务'} / {formConfig.groups.length} 个分组，{rulesCount} 条识别规则
            </span>
            <button disabled={loading || !activeTask} onClick={saveResults}>
              <Save size={16} />
              保存修改
            </button>
            <button disabled={loading || !activeTask} onClick={() => validateTask()}>
              <AlertTriangle size={16} />
              校验异常
            </button>
          </div>
        </div>

        <div className="form-workbench">
          <nav className="group-nav">
            {formConfig.groups.map((group) => (
              <button
                key={group.group_id}
                onClick={() => scrollToGroup(group.group_id)}
              >
                <span>{group.group_name}</span>
                <small>{group.fields.length} 项</small>
              </button>
            ))}
          </nav>

          <div className="all-fields">
            {formConfig.groups.length === 0 ? (
              <div className="empty">字段配置加载中。</div>
            ) : (
              formConfig.groups.map((group) => (
                <section className="form-group-section" id={`form-group-${group.group_id}`} key={group.group_id}>
                  <div className="form-group-title">
                    <h3>{group.group_name}</h3>
                    <span>{group.fields.length} 项</span>
                  </div>
                  <div className="field-grid">
                    {group.fields.filter((field) => field.component_type !== 'hidden').map((field) => (
                      <div
                        className={`field-card ${['table', 'manual_table', 'textarea'].includes(field.component_type) ? 'wide' : ''}`}
                        id={`field-${field.field_id}`}
                        key={field.field_id}
                      >
                        <span>
                          {field.field_name}
                          {field.required ? <b>必填</b> : null}
                        </span>
                        {renderFieldControl(field)}
                        {!field.manual_only ? (
                          <div className="field-actions">
                            <button
                              disabled={!fieldResults[field.field_id]}
                              onClick={(event) => {
                                event.preventDefault();
                                setActiveSource(fieldResults[field.field_id]);
                              }}
                              type="button"
                            >
                              <Info size={14} />
                              {fieldResults[field.field_id]?.source_text || fieldResults[field.field_id]?.source_section ? '来源' : '详情'}
                            </button>
                            {fieldResults[field.field_id] ? (
                              <ConfidenceBadge score={fieldResults[field.field_id]?.confidence_score} />
                            ) : null}
                          </div>
                        ) : null}
                        <small>
                          {field.field_type}
                          {field.max_length ? ` / ${field.max_length}字以内` : ''}
                        </small>
                      </div>
                    ))}
                  </div>
                </section>
              ))
            )}
          </div>
        </div>
      </section>

      <div className="bottom-actions">
        <button disabled={loading || !activeTask} onClick={saveResults}>
          保存
        </button>
        <button disabled={loading || !activeTask} onClick={() => validateTask()}>
          提交
        </button>
        <button className="ghost" type="button">
          关闭
        </button>
      </div>

      <button className="chat-fab" onClick={() => setChatOpen(true)} title="智能问答" type="button">
        <Bot size={24} />
      </button>

      {knowledgeOpen ? (
        <aside className="modal-backdrop">
          <section className="knowledge-modal">
            <div className="manual-edit-header">
              <div>
                <p className="eyebrow">Knowledge Base</p>
                <h2>知识库</h2>
              </div>
              <button type="button" className="ghost" onClick={() => setKnowledgeOpen(false)}>
                <X size={16} />
                关闭
              </button>
            </div>
            <label className="knowledge-upload">
              <UploadCloud size={18} />
              {knowledgeUploading ? '正在构建...' : '上传 Word 文档'}
              <input
                accept=".doc,.docx"
                disabled={knowledgeUploading}
                type="file"
                onChange={(event) => {
                  uploadKnowledgeDocument(event.target.files?.[0]);
                  event.target.value = '';
                }}
              />
            </label>
            <div className="knowledge-list">
              {knowledgeDocs.length === 0 ? (
                <div className="empty">暂无知识库文档，请上传 .doc 或 .docx 文件。</div>
              ) : (
                knowledgeDocs.map((doc) => (
                  <article className="knowledge-item" key={doc.document_id}>
                    <strong>{doc.file_name}</strong>
                    <span>{doc.chunk_count} 个切片 / {formatFileSize(doc.file_size)}</span>
                    <small>{formatTime(doc.uploaded_at)}</small>
                  </article>
                ))
              )}
            </div>
          </section>
        </aside>
      ) : null}

      {chatOpen ? (
        <aside className="chat-panel">
          <div className="chat-header">
            <div>
              <p className="eyebrow">RAG Assistant</p>
              <h2>智能问答</h2>
            </div>
            <button className="ghost" onClick={() => setChatOpen(false)} type="button">
              <X size={16} />
            </button>
          </div>
          <section className="retrieval-box">
            <div className="retrieval-title">
              <MessageCircle size={16} />
              检索内容
            </div>
            {chatSources.length === 0 ? (
              <div className="empty">提问后这里会显示匹配到的知识库片段。</div>
            ) : (
              chatSources.map((source) => (
                <article className="source-snippet" key={source.chunk_id}>
                  <strong>{source.file_name}</strong>
                  <span>{source.title}</span>
                  <p>{source.content}</p>
                </article>
              ))
            )}
          </section>
          <section className="chat-messages">
            {chatMessages.length === 0 ? (
              <div className="empty">可以询问已上传文档中的项目背景、财务指标、风险情况等内容。</div>
            ) : (
              chatMessages.map((item, index) => (
                <div className={`chat-message ${item.role}`} key={`${item.role}-${index}`}>
                  {item.content}
                </div>
              ))
            )}
            {chatLoading ? <div className="chat-message assistant">正在检索并生成回答...</div> : null}
          </section>
          <form className="chat-input" onSubmit={askKnowledge}>
            <input
              disabled={chatLoading}
              placeholder="输入问题，例如：项目主要风险有哪些？"
              value={chatQuestion}
              onChange={(event) => setChatQuestion(event.target.value)}
            />
            <button disabled={chatLoading || !chatQuestion.trim()} type="submit">
              <Send size={16} />
            </button>
          </form>
        </aside>
      ) : null}

      {manualTableModal ? (
        <aside className="modal-backdrop">
          <form className="manual-edit-modal" onSubmit={submitManualTableModal}>
            <div className="manual-edit-header">
              <div>
                <p className="eyebrow">{manualTableSchema(manualTableModal.field).title}</p>
                <h2>
                  {manualTableModal.mode === 'add' ? `新增${manualTableSchema(manualTableModal.field).itemName}`
                    : manualTableModal.mode === 'edit' ? `修改${manualTableSchema(manualTableModal.field).itemName}`
                      : `查看${manualTableSchema(manualTableModal.field).itemName}`}
                </h2>
              </div>
              <button type="button" className="ghost" onClick={() => setManualTableModal(null)}>
                关闭
              </button>
            </div>

            <div className="manual-edit-grid">
              {manualTableSchema(manualTableModal.field).fields.map((item) => (
                <label
                  className={item.wide || item.type === 'file' || item.type === 'textarea' ? 'manual-summary-field' : ''}
                  key={item.name}
                >
                  <span>{item.label}</span>
                  {item.type === 'select' ? (
                    <select
                      disabled={manualTableModal.mode === 'view'}
                      value={manualTableModal.form[item.name] ?? ''}
                      onChange={(event) => updateManualModalForm(item.name, event.target.value)}
                    >
                      <option value="">请选择</option>
                      {(item.options ?? []).map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  ) : item.type === 'textarea' ? (
                    <textarea
                      disabled={manualTableModal.mode === 'view'}
                      placeholder={item.placeholder ?? ''}
                      value={manualTableModal.form[item.name] ?? ''}
                      onChange={(event) => updateManualModalForm(item.name, event.target.value)}
                    />
                  ) : item.type === 'file' ? (
                    <>
                      <input
                        accept=".doc,.docx,.pdf,.xls,.xlsx,.ppt,.pptx"
                        disabled={manualTableModal.mode === 'view'}
                        type="file"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) {
                            uploadManualAttachment(file, item.name);
                          }
                        }}
                      />
                      <small>{manualTableModal.form[item.name] || '未选择附件'}</small>
                    </>
                  ) : (
                    <input
                      disabled={manualTableModal.mode === 'view'}
                      inputMode={item.type === 'number' ? 'decimal' : undefined}
                      placeholder={item.placeholder ?? ''}
                      type={item.type === 'number' ? 'text' : item.type}
                      value={manualTableModal.form[item.name] ?? ''}
                      onChange={(event) => updateManualModalForm(item.name, event.target.value)}
                    />
                  )}
                </label>
              ))}
            </div>

            <div className="manual-edit-actions">
              <button type="button" className="ghost" onClick={() => setManualTableModal(null)}>
                取消
              </button>
              {manualTableModal.mode !== 'view' ? (
                <button type="submit">
                  保存
                </button>
              ) : null}
            </div>
          </form>
        </aside>
      ) : null}

      {activeSource ? (
        <aside className="source-drawer">
          <div className="source-card">
            <div className="task-header">
              <div>
                <p className="eyebrow">字段来源</p>
                <h2>{activeSource.field_name}</h2>
              </div>
              <button className="ghost" onClick={() => setActiveSource(null)}>
                关闭
              </button>
            </div>
            <dl>
              <dt>当前填充值</dt>
              <dd>{activeSource.field_value}</dd>
              <dt>来源预览</dt>
              <dd>
                {activeSource.source_section || activeSource.source_text ? (
                  <button className="link-button" onClick={() => openSourcePreview(activeSource)}>
                    {activeSource.source_section || '查看原文预览'}
                  </button>
                ) : (
                  '无明确来源'
                )}
              </dd>
              <dt>处理方式</dt>
              <dd>{activeSource.generate_type}</dd>
              <dt>置信度</dt>
              <dd>
                <ConfidenceBadge score={activeSource.confidence_score} />
              </dd>
              <dt>评分依据</dt>
              <dd>
                {parseConfidenceReasons(activeSource.confidence_reasons).length > 0 ? (
                  <ul className="score-reasons">
                    {parseConfidenceReasons(activeSource.confidence_reasons).map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                ) : (
                  '无'
                )}
              </dd>
              <dt>异常提示</dt>
              <dd>{activeSource.exception_message || '无'}</dd>
              <dt>原文片段</dt>
              <dd className="source-text">{activeSource.source_text || '无原文片段'}</dd>
              <dt>精确锚点</dt>
              <dd className="source-text">{activeSource.exact_quote || '无精确锚点'}</dd>
              <dt>锚点位置</dt>
              <dd>
                {activeSource.source_paragraph_index
                  ? `paragraph_index=${activeSource.source_paragraph_index}，char_start=${activeSource.char_start ?? '-'}，char_end=${activeSource.char_end ?? '-'}`
                  : '无锚点位置'}
              </dd>
            </dl>
          </div>
        </aside>
      ) : null}

      {sourcePreview ? (
        <aside className="source-drawer">
          <div className="source-card preview-card">
            <div className="task-header">
              <div>
                <p className="eyebrow">原文预览</p>
                <h2>{sourcePreview.result.field_name}</h2>
              </div>
              <button className="ghost" onClick={() => setSourcePreview(null)}>
                关闭
              </button>
            </div>
            <dl>
              <dt>来源章节</dt>
              <dd>{sourcePreview.result.source_section || '无明确来源'}</dd>
              <dt>置信度评分</dt>
              <dd>
                <ConfidenceBadge score={sourcePreview.result.confidence_score} />
              </dd>
              <dt>评分依据</dt>
              <dd>
                {parseConfidenceReasons(sourcePreview.result.confidence_reasons).length > 0 ? (
                  <ul className="score-reasons">
                    {parseConfidenceReasons(sourcePreview.result.confidence_reasons).map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                ) : (
                  '无'
                )}
              </dd>
              <dt>系统引用文字</dt>
              <dd className="source-text">{sourcePreview.result.source_text || '无原文片段'}</dd>
              <dt>精确锚点</dt>
              <dd className="source-text">{sourcePreview.result.exact_quote || '无精确锚点'}</dd>
              <dt>锚点位置</dt>
              <dd>
                {sourcePreview.result.source_paragraph_index
                  ? `paragraph_index=${sourcePreview.result.source_paragraph_index}，char_start=${sourcePreview.result.char_start ?? '-'}，char_end=${sourcePreview.result.char_end ?? '-'}`
                  : '无锚点位置'}
              </dd>
              <dt>章节原文</dt>
              <dd className="word-preview-frame-wrap">
                <iframe
                  className="word-preview-frame"
                  srcDoc={sourcePreview.html || ''}
                  title="Word 原文预览"
                />
              </dd>
              {sourcePreview.section ? null : (
                <>
                  <dt>解析章节匹配</dt>
                  <dd>未在当前解析结果中匹配到对应章节，已加载完整 Word 原文预览。</dd>
                </>
              )}
            </dl>
          </div>
        </aside>
      ) : null}

      {showExceptions ? (
        <aside className="source-drawer">
          <div className="source-card exception-card">
            <div className="task-header">
              <div>
                <p className="eyebrow">异常项</p>
                <h2>{exceptions.length} 个异常</h2>
              </div>
              <button className="ghost" onClick={() => setShowExceptions(false)}>
                关闭
              </button>
            </div>
            {exceptions.length === 0 ? (
              <div className="empty">当前没有发现异常项。</div>
            ) : (
              <div className="exception-list">
                {exceptions.map((item, index) => (
                  <button
                    className="exception-item"
                    key={`${item.field_id}-${item.exception_type}-${index}`}
                    onClick={() => {
                      setShowExceptions(false);
                      document.getElementById(`field-${item.field_id}`)?.scrollIntoView({
                        behavior: 'smooth',
                        block: 'center',
                      });
                    }}
                  >
                    <strong>{item.field_name}</strong>
                    <span>{item.field_group} / {item.message}</span>
                    <small>{item.suggestion}</small>
                  </button>
                ))}
              </div>
            )}
          </div>
        </aside>
      ) : null}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
