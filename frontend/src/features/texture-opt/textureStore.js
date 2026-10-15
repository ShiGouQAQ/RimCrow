// src/stores/textureStore.js
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { useAppStore } from '../../app/stores/appStore'
import { toast, checkResult, showUserErrorToast } from '../../shared/lib/common'
import { useTaskStore } from '../../app/stores/taskStore'
import { t, translateMessagePayload } from '../../shared/i18n.js'

const toInt = (value, fallback = 0) => {
  const num = Number(value)
  return Number.isFinite(num) ? num : fallback
}

export const getTextureScaleBreakdownItems = (stat = {}, kind = '') => (
  (Array.isArray(stat?.scale_breakdown) ? stat.scale_breakdown : [])
    .filter(item => String(item?.kind || 'keep_original') === kind && toInt(item?.count) > 0)
)

export const sumTextureScaleBreakdownItems = (items = []) => (
  Array.isArray(items) ? items.reduce((total, item) => total + toInt(item?.count), 0) : 0
)

export const getTextureKeepOriginalReasonItems = (stat = {}, labels = {}) => {
  const total = toInt(stat?.keep_original_count)
  const mask = toInt(stat?.keep_original_mask_count)
  const range = toInt(stat?.keep_original_range_count ?? stat?.skip_small_count)
  const normal = stat?.keep_original_normal_count == null
    ? Math.max(0, total - mask - range)
    : toInt(stat.keep_original_normal_count)
  return [
    { label: labels.normal || '不缩放', count: normal },
    { label: labels.mask || '遮罩贴图不缩放', count: mask },
    { label: labels.range || '超范围不缩放', count: range },
  ].filter(item => item.count > 0)
}

export const useTextureStore = defineStore('texture', () => {
  const appStore = useAppStore()
  const taskStore = useTaskStore()
  let toolStatusRefreshTimer = null

  // 三档转换工具（标准 todds / 中等 nvtt / 快速 texconv）的展示顺序与工具名
  const ENCODER_TIERS = [
    { backend: 'todds', tool: 'todds' },
    { backend: 'nvtt', tool: 'nvtt' },
    { backend: 'texconv', tool: 'texconv' },
  ]

  const createEmptyTextureStat = () => ({
    mod_path: '',
    mod_name: '',
    package_id: '',
    store: '',
    path_hash: '',
    mod_instance_key: '',
    source_total_count: 0,
    source_total_bytes: 0,
    output_total_count: 0,
    output_total_bytes: 0,
    dds_output_count: 0,
    dds_output_bytes: 0,
    zstd_output_count: 0,
    zstd_output_bytes: 0,
    current_output_count: 0,
    current_output_bytes: 0,
    external_orphan_output_count: 0,
    external_orphan_output_bytes: 0,
    generate_required_count: 0,
    excluded_count: 0,
    skip_small_count: 0,
    skipped_mask_count: 0,
    unsupported_source_count: 0,
    unreadable_source_count: 0,
    scan_failed_count: 0,
    scaled_count: 0,
    fallback_scaled_count: 0,
    keep_original_count: 0,
    keep_original_normal_count: 0,
    keep_original_mask_count: 0,
    keep_original_range_count: 0,
    source_vram_bytes_est: 0,
    output_vram_bytes_est: 0,
    vram_saving_bytes_est: 0,
    combined_total_bytes: 0,
    source_bytes_share_pct: 0,
    output_bytes_share_pct: 0,
    dds_output_bytes_share_pct: 0,
    zstd_output_bytes_share_pct: 0,
    combined_bytes_share_pct: 0,
    scale_breakdown: [],
    projection_basis: [],
    engine_unsupported_preview: [],
    mod_count: 0,
  })

  const normalizeTextureStat = (raw = {}, { includeModCount = false } = {}) => {
    const base = createEmptyTextureStat()
    const sourceTotalCount = toInt(raw.source_total_count ?? raw.source_count ?? base.source_total_count)
    const sourceTotalBytes = toInt(raw.source_total_bytes ?? base.source_total_bytes)
    const outputTotalCount = toInt(raw.output_total_count ?? base.output_total_count)
    const outputTotalBytes = toInt(raw.output_total_bytes ?? base.output_total_bytes)
    const currentOutputCount = toInt(raw.current_output_count ?? raw.up_to_date_count ?? base.current_output_count)
    const currentOutputBytes = toInt(raw.current_output_bytes ?? base.current_output_bytes)

    const normalized = {
      ...base,
      ...raw,
      mod_path: String(raw.mod_path || base.mod_path),
      mod_name: String(raw.mod_name || base.mod_name),
      package_id: String(raw.package_id || base.package_id),
      store: String(raw.store || base.store),
      path_hash: String(raw.path_hash || base.path_hash),
      mod_instance_key: String(raw.mod_instance_key || raw.path_hash || raw.mod_path || base.mod_instance_key),
      source_total_count: sourceTotalCount,
      source_total_bytes: sourceTotalBytes,
      output_total_count: outputTotalCount,
      output_total_bytes: outputTotalBytes,
      dds_output_count: toInt(raw.dds_output_count ?? (raw.zstd_output_count == null ? outputTotalCount : base.dds_output_count)),
      dds_output_bytes: toInt(raw.dds_output_bytes ?? (raw.zstd_output_bytes == null ? outputTotalBytes : base.dds_output_bytes)),
      zstd_output_count: toInt(raw.zstd_output_count ?? base.zstd_output_count),
      zstd_output_bytes: toInt(raw.zstd_output_bytes ?? base.zstd_output_bytes),
      current_output_count: currentOutputCount,
      current_output_bytes: currentOutputBytes,
      external_orphan_output_count: toInt(raw.external_orphan_output_count ?? base.external_orphan_output_count),
      external_orphan_output_bytes: toInt(raw.external_orphan_output_bytes ?? base.external_orphan_output_bytes),
      generate_required_count: toInt(raw.generate_required_count ?? raw.pending_count ?? base.generate_required_count),
      excluded_count: toInt(raw.excluded_count ?? base.excluded_count),
      skip_small_count: toInt(raw.skip_small_count ?? raw.skipped_small_count ?? base.skip_small_count),
      skipped_mask_count: toInt(raw.skipped_mask_count ?? base.skipped_mask_count),
      unsupported_source_count: toInt(raw.unsupported_source_count ?? raw.unsupported_count ?? base.unsupported_source_count),
      unreadable_source_count: toInt(raw.unreadable_source_count ?? base.unreadable_source_count),
      scan_failed_count: toInt(raw.scan_failed_count ?? base.scan_failed_count),
      scaled_count: toInt(raw.scaled_count ?? base.scaled_count),
      fallback_scaled_count: toInt(raw.fallback_scaled_count ?? base.fallback_scaled_count),
      keep_original_count: toInt(raw.keep_original_count ?? base.keep_original_count),
      keep_original_normal_count: toInt(raw.keep_original_normal_count ?? base.keep_original_normal_count),
      keep_original_mask_count: toInt(raw.keep_original_mask_count ?? base.keep_original_mask_count),
      keep_original_range_count: toInt(raw.keep_original_range_count ?? base.keep_original_range_count),
      source_vram_bytes_est: toInt(raw.source_vram_bytes_est ?? base.source_vram_bytes_est),
      output_vram_bytes_est: toInt(raw.output_vram_bytes_est ?? base.output_vram_bytes_est),
      vram_saving_bytes_est: toInt(
        raw.vram_saving_bytes_est
        ?? (toInt(raw.source_vram_bytes_est ?? 0) - toInt(raw.output_vram_bytes_est ?? 0))
      ),
      combined_total_bytes: toInt(raw.combined_total_bytes ?? (sourceTotalBytes + outputTotalBytes)),
      source_bytes_share_pct: Number(raw.source_bytes_share_pct ?? base.source_bytes_share_pct) || 0,
      output_bytes_share_pct: Number(raw.output_bytes_share_pct ?? base.output_bytes_share_pct) || 0,
      dds_output_bytes_share_pct: Number(raw.dds_output_bytes_share_pct ?? base.dds_output_bytes_share_pct) || 0,
      zstd_output_bytes_share_pct: Number(raw.zstd_output_bytes_share_pct ?? base.zstd_output_bytes_share_pct) || 0,
      combined_bytes_share_pct: Number(raw.combined_bytes_share_pct ?? base.combined_bytes_share_pct) || 0,
      scale_breakdown: Array.isArray(raw.scale_breakdown) ? raw.scale_breakdown : [],
      projection_basis: Array.isArray(raw.projection_basis) ? raw.projection_basis : [],
      engine_unsupported_preview: Array.isArray(raw.engine_unsupported_preview) ? raw.engine_unsupported_preview : [],
    }

    if (includeModCount) {
      normalized.mod_count = toInt(raw.mod_count ?? base.mod_count)
    } else {
      delete normalized.mod_count
    }
    return normalized
  }

  const normalizeTextureRows = (rows = []) => (
    Array.isArray(rows) ? rows.map(item => normalizeTextureStat(item)) : []
  )

  const getScaleLabelSortValue = (label) => {
    const match = String(label || '').match(/(\d+(?:\.\d+)?)\s*%/)
    return match ? Number(match[1]) : Number.POSITIVE_INFINITY
  }

  const sumScaleBreakdown = (rows = []) => {
    const counts = new Map()
    rows.forEach(row => {
      ;(Array.isArray(row.scale_breakdown) ? row.scale_breakdown : []).forEach(item => {
        if (!item || typeof item !== 'object') return
        const kind = String(item.kind || 'keep_original')
        const label = String(item.label || t('ui.texture_opt.scale.original_size', '原尺寸'))
        const key = `${kind}\n${label}`
        counts.set(key, {
          kind,
          label,
          count: (counts.get(key)?.count || 0) + toInt(item.count),
        })
      })
    })
    const order = { scaled: 0, fallback: 1, keep_original: 2 }
    return Array.from(counts.values())
      .filter(item => item.count > 0)
      .sort((a, b) => (
        (order[a.kind] ?? 99) - (order[b.kind] ?? 99)
        || getScaleLabelSortValue(a.label) - getScaleLabelSortValue(b.label)
        || a.label.localeCompare(b.label)
      ))
  }

  const rebuildSummaryFromRows = (rows = []) => {
    const summary = createEmptyTextureStat()
    summary.mod_count = rows.length
    const numericKeys = Object.keys(summary).filter(key => (
      typeof summary[key] === 'number'
      && !['mod_count', 'source_bytes_share_pct', 'output_bytes_share_pct', 'combined_bytes_share_pct'].includes(key)
    ))
    rows.forEach(row => {
      numericKeys.forEach(key => {
        summary[key] += toInt(row[key])
      })
    })
    summary.scale_breakdown = sumScaleBreakdown(rows)
    summary.combined_total_bytes = summary.source_total_bytes + summary.output_total_bytes
    summary.vram_saving_bytes_est = summary.source_vram_bytes_est - summary.output_vram_bytes_est
    summary.engine_unsupported_preview = rows
      .flatMap(row => Array.isArray(row.engine_unsupported_preview) ? row.engine_unsupported_preview : [])
      .slice(0, 12)
    return normalizeTextureStat(summary, { includeModCount: true })
  }

  const normalizeFailedItem = (raw = {}) => ({
    package_id: String(raw.package_id || ''),
    mod_path: String(raw.mod_path || ''),
    mod_name: String(raw.mod_name || ''),
    rel_path: String(raw.rel_path || ''),
    file_path: String(raw.file_path || ''),
    error: String(raw.error || ''),
    todds_log_path: String(raw.todds_log_path || ''),
  })

  const normalizeResultHistoryItem = (raw = {}) => ({
    ...raw,
    task_id: String(raw.task_id || ''),
    action: String(raw.action || ''),
    result_path: String(raw.result_path || ''),
    summary: normalizeTextureStat(raw.summary || {}, { includeModCount: true }),
    mods: normalizeTextureRows(raw.mods || []),
    failed_items: Array.isArray(raw.failed_items) ? raw.failed_items.map(normalizeFailedItem) : [],
    failed_count: toInt(raw.failed_count ?? raw.failed ?? raw.failed_items?.length ?? 0),
    mod_paths: Array.isArray(raw.mod_paths) ? raw.mod_paths.map(item => String(item || '')) : [],
    mod_targets: Array.isArray(raw.mod_targets) ? raw.mod_targets.map(item => normalizeTextureStat(item || {})) : [],
    todds_log_path: String(raw.todds_log_path || ''),
    created_at: Number(raw.created_at || 0),
    updated_at: Number(raw.updated_at || 0),
  })

  const getRowKey = (item = {}) => (
    String(item.mod_instance_key || item.path_hash || item.mod_path || '')
  )
  const getSingleTargetKey = (target = {}) => (
    String(target?.mod_instance_key || target?.path_hash || target?.mod_path || '')
  )

  // === 状态 State ===
  const isAnalyzing = ref(false)
  const isOptimizing = ref(false)
  const currentTaskId = ref('')
  const currentAnalysisTaskId = ref('')
  const currentOptimizationTaskId = ref('')
  const lastFinishedProgress = ref(null)
  const lastTargetPackageIds = ref([])
  const lastTargetScope = ref('active')
  const lastOptimizationAction = ref('')
  const lastSingleModTargetKey = ref('')

  // 核心数据源
  const modsData = ref([])        // 分析后返回的所有 Mod 数据
  const globalSummary = ref(normalizeTextureStat({}, { includeModCount: true }))
  const resultHistory = ref([])
  const textureExclusions = ref({ schema_version: 1, mods: [], files: [] })
  const isResultDrawerOpen = ref(false)
  const selectedResultPath = ref('')

  const buildProgressState = (task) => {
    if (!task) {
      return {
        percent: 0,
        message: t('common.status.ready', '就绪'),
        details: {
          local_started_at: 0,
          local_finished_at: 0,
          local_total_elapsed_ms: 0,
          local_status: '',
        }
      }
    }
    const startedAt = Number(task.metrics?.task_created_at || task.joinedAt || 0)
    const finishedAt = ['success', 'failed', 'cancelled'].includes(task.status) ? Number(task.updatedAt || 0) : 0
    const totalElapsedMs = finishedAt
      ? Number(task.metrics?.elapsed_ms || Math.max(0, finishedAt - startedAt))
      : Math.max(0, Date.now() - startedAt)
    return {
      percent: Number(task.progress || 0),
      message: translateMessagePayload(task, task.message) || t('tasks.message.processing', '处理中...'),
      details: {
        ...(task.metrics || {}),
        local_started_at: startedAt,
        local_finished_at: finishedAt,
        local_total_elapsed_ms: totalElapsedMs,
        local_status: task.status,
      }
    }
  }

  const currentTask = computed(() => {
    const taskId = currentOptimizationTaskId.value || currentAnalysisTaskId.value || currentTaskId.value
    return (
      (taskId ? taskStore.getTask(taskId) : null)
      || taskStore.getLatestTaskByType(['texture-opt', 'texture-opt-analyze'])
      || null
    )
  })

  const progressState = computed(() => {
    const task = currentTask.value
    return task ? buildProgressState(task) : (lastFinishedProgress.value || buildProgressState(null))
  })

  // 视图控制
  const viewMode = ref('ALL') // 'ALL' | 'PNG' | 'DDS' | 'ZSTD'

  // 工具就绪状态
  const toolStatus = ref({
    available: false,
    resolved_path: '',
    message: '',
    message_key: '',
    message_params: {},
  })

  // 三档转换工具状态（texture_get_encoder_status 回包的 data 层：encoders / machine / plan）
  const encoderStatus = ref({
    encoders: {},
    machine: null,
    plan: null,
  })

  // === 动作 Actions ===
  const bindTaskId = (payload, kind) => {
    const taskId = payload?.task_id || payload?.id || ''
    currentTaskId.value = taskId
    lastFinishedProgress.value = null
    if (kind === 'analyze') {
      currentAnalysisTaskId.value = taskId
    } else if (kind === 'optimize') {
      currentOptimizationTaskId.value = taskId
    }
    return taskId
  }

  const applyReturnedTaskState = (payload) => {
    if (!payload) return
    const task = taskStore.upsertTask({
      id: payload.task_id || payload.id,
      type: payload.type || (payload.action === 'analyze' ? 'texture-opt-analyze' : 'texture-opt'),
      status: payload.status || 'pending',
      progress: payload.progress || 0,
      message: payload.message || '',
      message_key: payload.message_key || '',
      message_params: payload.message_params || {},
      metrics: payload.metrics || {},
      timestamp: payload.updated_at || Date.now(),
    })
    if (task && ['success', 'failed', 'cancelled'].includes(task.status)) {
      lastFinishedProgress.value = buildProgressState(task)
    }
  }

  const markTaskCancelling = () => {
    const task = currentTask.value
    if (!task) return
    taskStore.upsertTask({
      ...task,
      status: 'running',
      message: t('tasks.message.cancelling_request', '正在尝试中止任务...'),
      message_key: 'tasks.message.cancelling_request',
      message_params: {},
      metrics: {
        ...(task.metrics || {}),
        phase: 'cancelling',
      },
      timestamp: Date.now(),
    })
  }

  const mergeTextureRows = (rows = []) => {
    const nextRows = [...modsData.value]
    const normalizedRows = normalizeTextureRows(rows)
    for (const row of normalizedRows) {
      const rowKey = getRowKey(row)
      const rowPath = String(row.mod_path || '').trim().toLowerCase()
      const existingIdx = nextRows.findIndex(item => (
        (!!rowKey && getRowKey(item) === rowKey)
        || (!!rowPath && String(item.mod_path || '').trim().toLowerCase() === rowPath)
      ))
      if (existingIdx !== -1) {
        nextRows[existingIdx] = row
      } else {
        nextRows.push(row)
      }
    }
    modsData.value = nextRows
    globalSummary.value = rebuildSummaryFromRows(nextRows)
  }

  const applySnapshotPayload = (payload = {}) => {
    const summary = payload?.summary
    const finalMods = payload?.final_mods
    const mods = payload?.mods

    if (!lastSingleModTargetKey.value && summary && typeof summary === 'object') {
      globalSummary.value = normalizeTextureStat(summary, { includeModCount: true })
    }
    if (Array.isArray(finalMods)) {
      if (lastSingleModTargetKey.value) {
        mergeTextureRows(finalMods)
      } else {
        modsData.value = normalizeTextureRows(finalMods)
      }
      return
    }
    if (Array.isArray(mods)) {
      modsData.value = normalizeTextureRows(mods)
    }
  }

  const upsertCurrentEntry = (entry) => {
    if (!entry) return
    const currentEntry = normalizeTextureStat(entry)
    const currentKey = getRowKey(currentEntry)
    const existingIdx = modsData.value.findIndex(item => getRowKey(item) === currentKey)
    if (existingIdx !== -1) {
      modsData.value[existingIdx] = currentEntry
    } else {
      modsData.value.push(currentEntry)
    }
  }

  const cleanedGenerateRequiredCount = (row) => {
    const mode = String(appStore.settings?.texture_opt?.process_mode || 'scaled_only_overwrite')
    const scaled = toInt(row.scaled_count) + toInt(row.fallback_scaled_count)
    const original = toInt(row.keep_original_count)
    if (mode === 'scaled_only_overwrite') return scaled
    return scaled + original
  }

  const clearGeneratedOutputsFromCurrentRows = ({ outputFormat = 'dds' } = {}) => {
    const normalizedOutputFormat = ['dds', 'zstd'].includes(String(outputFormat || '').toLowerCase())
      ? String(outputFormat || '').toLowerCase()
      : 'dds'
    const clearDds = normalizedOutputFormat === 'dds'
    const clearZstd = normalizedOutputFormat === 'zstd'
    const targetIds = new Set(lastTargetPackageIds.value.map(item => String(item || '').trim().toLowerCase()).filter(Boolean))
    const shouldUpdateAll = lastTargetScope.value === 'all' || targetIds.size === 0
    const outputMode = String(appStore.settings?.texture_opt?.output_format || 'dds')
    const singleTargetKey = String(lastSingleModTargetKey.value || '')
    let changed = false
    const nextRows = modsData.value.map(row => {
      const packageId = String(row.package_id || '').trim().toLowerCase()
      const rowKey = getRowKey(row)
      if (singleTargetKey) {
        if (rowKey !== singleTargetKey) return row
      } else if (!shouldUpdateAll && !targetIds.has(packageId)) {
        return row
      }
      changed = true
      const nextDdsCount = clearDds ? 0 : toInt(row.dds_output_count)
      const nextDdsBytes = clearDds ? 0 : toInt(row.dds_output_bytes)
      const nextZstdCount = clearZstd ? 0 : toInt(row.zstd_output_count)
      const nextZstdBytes = clearZstd ? 0 : toInt(row.zstd_output_bytes)
      const nextCurrentCount = outputMode === 'zstd' ? nextZstdCount : nextDdsCount
      const nextCurrentBytes = outputMode === 'zstd' ? nextZstdBytes : nextDdsBytes
      const next = {
        ...row,
        dds_output_count: nextDdsCount,
        dds_output_bytes: nextDdsBytes,
        zstd_output_count: nextZstdCount,
        zstd_output_bytes: nextZstdBytes,
        output_total_count: nextDdsCount + nextZstdCount,
        output_total_bytes: nextDdsBytes + nextZstdBytes,
        current_output_count: nextCurrentCount,
        current_output_bytes: nextCurrentBytes,
        external_orphan_output_count: 0,
        external_orphan_output_bytes: 0,
        output_vram_bytes_est: nextCurrentCount > 0 ? toInt(row.output_vram_bytes_est) : 0,
        dds_output_bytes_share_pct: clearDds ? 0 : Number(row.dds_output_bytes_share_pct || 0),
        zstd_output_bytes_share_pct: clearZstd ? 0 : Number(row.zstd_output_bytes_share_pct || 0),
      }
      next.generate_required_count = cleanedGenerateRequiredCount(next)
      next.action_required_count = next.generate_required_count
      next.combined_total_bytes = toInt(next.source_total_bytes) + toInt(next.output_total_bytes)
      next.vram_saving_bytes_est = toInt(next.source_vram_bytes_est) - toInt(next.output_vram_bytes_est)
      next.output_bytes_share_pct = 0
      return normalizeTextureStat(next)
    })
    if (!changed) return
    modsData.value = nextRows
    globalSummary.value = rebuildSummaryFromRows(nextRows)
  }

  const clearSourcelessOutputsFromSingleRow = () => {
    const singleTargetKey = String(lastSingleModTargetKey.value || '')
    if (!singleTargetKey) return
    let changed = false
    const nextRows = modsData.value.map(row => {
      if (getRowKey(row) !== singleTargetKey) return row
      changed = true
      return normalizeTextureStat({
        ...row,
        external_orphan_output_count: 0,
        external_orphan_output_bytes: 0,
      })
    })
    if (!changed) return
    modsData.value = nextRows
    globalSummary.value = rebuildSummaryFromRows(nextRows)
  }

  const bindIncomingTaskIfNeeded = (payload) => {
    const { id, type } = payload || {}
    if (!id) return false
    if (id === currentAnalysisTaskId.value || id === currentOptimizationTaskId.value || id === currentTaskId.value) {
      return true
    }

    if (type === 'texture-opt-analyze' && isAnalyzing.value && !currentAnalysisTaskId.value) {
      currentAnalysisTaskId.value = id
      currentTaskId.value = id
      return true
    }

    if (type === 'texture-opt' && isOptimizing.value && !currentOptimizationTaskId.value) {
      currentOptimizationTaskId.value = id
      currentTaskId.value = id
      return true
    }

    return false
  }

  // 转换工具（todds/texconv）下载完成时刷新全部就绪状态：
  // scheduleToolStatusRefresh 的刷新链同时覆盖 toolStatus、三档状态与本机方案
  const isTextureToolDownloadPayload = (payload = {}) => {
    const filename = String(payload.metrics?.filename || '').toLowerCase()
    const filePath = String(payload.metrics?.file_path || '').toLowerCase()
    return ['todds', 'texconv'].some(name => filename.startsWith(name) || filePath.includes(name))
  }

  const scheduleToolStatusRefresh = (attempt = 0) => {
    if (toolStatusRefreshTimer) {
      clearTimeout(toolStatusRefreshTimer)
      toolStatusRefreshTimer = null
    }
    toolStatusRefreshTimer = window.setTimeout(async () => {
      await checkToolStatus()
      // 编码器状态与工具状态一起刷新，下载完成后三行状态同步更新
      await checkEncoderStatus()
      if (!toolStatus.value.available && attempt < 5) {
        scheduleToolStatusRefresh(attempt + 1)
      }
    }, attempt === 0 ? 900 : 1200)
  }

  // 1. 检查后端工具状态
  const checkToolStatus = async () => {
    if (!window.pywebview) return
    try {
      const res = await window.pywebview.api.texture_get_env_status(appStore.settings.texture_opt)
      if (checkResult(res, t('check.texture.tool_status', '检查贴图工具'), false)) {
        toolStatus.value = {
          ...(res.data || {}),
          message: translateMessagePayload(res.data || {}, res.data?.message || ''),
        }
      }
    } catch (e) {
      console.error('检查贴图工具状态失败:', e)
    }
  }

  // 2.5 档位中文名（面板与设置页共用）
  const encoderDisplayLabel = (backend) => {
    if (backend === 'auto') return t('dialog.texture_opt.encoder_backend.auto', '自动（按本机推荐）')
    if (backend === 'nvtt') return t('dialog.texture_opt.encoder_backend.nvtt', '中等')
    if (backend === 'texconv') return t('dialog.texture_opt.encoder_backend.texconv', '快速')
    return t('dialog.texture_opt.encoder_backend.todds', '标准')
  }

  // 2.6 按机器容量给出的编码方案（get_encoder_status 回包的 machine/plan 层，对用户可见）。
  // 档位与分片文案都按 effective_backend（用户所选档位，缺失时回退 auto 落点）解析，
  // 保证「生效」一行反映实际将运行的编码器，而不是恒显 auto 的落点。
  const machinePlan = computed(() => {
    const machine = encoderStatus.value?.machine
    const plan = encoderStatus.value?.plan
    if (!machine || !plan) return null
    const gpuOk = machine.gpu_directcompute === true
    const gpuUnknown = machine.gpu_directcompute == null
    const effectiveBackend = String(plan.effective_backend || plan.backend || 'todds')
    const workersText = effectiveBackend === 'todds'
      ? t('dialog.texture_opt.plan_workers_todds', 'todds 使用内置多线程，无分片设置')
      : effectiveBackend === 'nvtt'
        ? t('dialog.texture_opt.plan_workers_nvtt', '编码分片 {n}（上限 6）', { n: Number(plan.nvtt_shards || 0) })
        : t('dialog.texture_opt.plan_workers', 'GPU 分片 {gpu} + CPU 分片 {cpu}', { gpu: plan.gpu_workers, cpu: plan.cpu_workers })
    return {
      gpuText: gpuUnknown
        ? t('dialog.texture_opt.plan_gpu_unknown', 'GPU 探测未完成（未安装快速档时属预期）')
        : gpuOk
          ? t('dialog.texture_opt.plan_gpu_ok', 'GPU 编码可用（DirectCompute 实测通过）')
          : t('dialog.texture_opt.plan_gpu_bad', 'GPU 编码不可用，透明通道贴图走 CPU'),
      recommendedLabel: encoderDisplayLabel(effectiveBackend),
      effectiveBackend,
      selectedBackend: String(plan.selected_backend || 'auto'),
      effectiveGpu: Number(plan.gpu_workers || 0),
      effectiveCpu: Number(plan.cpu_workers || 0),
      nvttShards: Number(plan.nvtt_shards || 0),
      source: String(plan.source || 'default'),
      overrides: plan.overrides || { gpu_workers: 0, cpu_workers: 0 },
      workersText,
      // reason 解释的是 auto 的落点取舍，用户显式选档时不再展示
      reason: String(plan.reason || ''),
      showReason: String(plan.selected_backend || 'auto') === 'auto',
      qualityNote: String(plan.quality_note || ''),
      texconvVersion: String(plan.texconv_version || ''),
      cpuLogical: Number(machine.cpu_logical || 0),
    }
  })

  // 生效分片数的来源徽标：手动设置 > 采样实测 > 默认推荐
  const planSourceLabel = computed(() => {
    const source = String(encoderStatus.value?.plan?.source || 'default')
    if (source === 'manual') return t('dialog.texture_opt.plan_source_manual', '手动设置')
    if (source === 'benchmark') return t('dialog.texture_opt.plan_source_benchmark', '采样实测')
    return t('dialog.texture_opt.plan_source_default', '默认推荐')
  })

  // 2.7 三档转换工具状态（纯读）
  const checkEncoderStatus = async () => {    if (!window.pywebview) return
    try {
      const res = await window.pywebview.api.texture_get_encoder_status(appStore.settings.texture_opt)
      if (checkResult(res, t('check.texture.encoder_status', '检查转换档位工具'), false)) {
        encoderStatus.value = { ...(res.data || {}) }
      }
    } catch (e) {
      console.error('检查转换档位工具状态失败:', e)
    }
  }

  // 2.8 采样基准（用户手动触发）与分片数手动覆盖
  const benchmarkRunning = ref(false)
  const benchmarkMessage = ref('')

  const runWorkerBenchmark = async () => {
    if (!window.pywebview || benchmarkRunning.value) return
    benchmarkRunning.value = true
    benchmarkMessage.value = t('toast.texture.benchmark_starting', '正在启动采样基准…')
    try {
      const res = await window.pywebview.api.texture_run_worker_benchmark(appStore.settings.texture_opt)
      if (!checkResult(res, t('check.texture.benchmark_start', '启动采样基准'))) {
        benchmarkRunning.value = false
        benchmarkMessage.value = ''
        return
      }
      const taskId = String(res.data?.task_id || '')
      if (!taskId) {
        benchmarkRunning.value = false
        return
      }
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1200))
        const poll = await window.pywebview.api.texture_get_worker_benchmark(taskId)
        if (!checkResult(poll, t('check.texture.benchmark_poll', '查询基准进度'), false)) break
        const payload = poll.data || {}
        benchmarkMessage.value = String(payload.message || '')
        const status = String(payload.status || '')
        if (status === 'completed') {
          toast.success(t('toast.texture.benchmark_done', '采样基准完成'))
          break
        }
        if (status === 'failed' || status === 'cancelled') {
          toast.error(benchmarkMessage.value || t('toast.texture.benchmark_failed', '采样基准未完成'))
          break
        }
      }
    } catch (e) {
      console.error('采样基准执行失败:', e)
    } finally {
      benchmarkRunning.value = false
      await checkEncoderStatus()
    }
  }

  const saveWorkerOverrides = async (gpuWorkers, cpuWorkers) => {
    if (!window.pywebview) return
    appStore.isLoading = true
    try {
      const res = await window.pywebview.api.texture_set_worker_overrides(Number(gpuWorkers) || 0, Number(cpuWorkers) || 0, appStore.settings.texture_opt)
      if (checkResult(res, t('check.texture.worker_overrides', '保存分片设置'))) {
        if (res.data) encoderStatus.value = { ...(res.data || {}) }
        toast.success(t('toast.texture.worker_overrides_saved', '分片设置已保存'))
      }
    } catch (e) {
      console.error('保存分片设置失败:', e)
    } finally {
      appStore.isLoading = false
    }
  }

  // 2.7 面板与设置页共用的三行状态；状态回包前 checked=false，只显示「检查中」不误报「不可用」
  const encoderRows = computed(() => {
    const encoders = encoderStatus.value?.encoders || {}
    return ENCODER_TIERS.map((tier) => {
      const info = encoders[tier.backend]
      const checked = !!info
      const available = !!info?.available
      return {
        backend: tier.backend,
        label: encoderDisplayLabel(tier.backend),
        checked,
        available,
        statusText: available
          ? t('dialog.texture_opt.encoder_available', '可用')
          : (checked ? t('dialog.texture_opt.encoder_unavailable', '不可用') : t('common.status.checking', '检查中')),
        detail: !checked
          ? ''
          : (available
            ? String(info.resolved_path || '')
            : (tier.backend === 'nvtt'
              ? t('dialog.texture_opt.encoder_missing_nvtt', '未检测到中等档所需的程序。可前往官方页面下载安装后返回此处。')
              : t('dialog.texture_opt.encoder_missing', '未在本机找到 {name} 程序。', { name: tier.tool }))),
        actionLabel: (checked && !available)
          ? (tier.backend === 'nvtt'
            ? t('dialog.texture_opt.encoder_open_site', '打开官网')
            : (tier.backend === 'texconv'
              ? t('dialog.texture_opt.download_texconv', '下载 texconv')
              : t('dialog.texture_opt.download_todds', '下载 todds')))
          : '',
      }
    })
  })

  // 2.8 下载缺失的转换工具；NVTT 不自动下载，改为打开官方页面
  const downloadEncoder = async (backend) => {
    if (!window.pywebview) return
    const target = String(backend || 'todds')
    const label = encoderDisplayLabel(target)
    const failureText = t('errors.texture.tool_download_failed', '启动贴图工具下载失败。请检查网络连接、代理设置和工具目录写入权限。')
    appStore.isLoading = true
    try {
      const res = await window.pywebview.api.texture_prepare_encoder_download(target, appStore.settings.texture_opt)
      if (res.status !== 'success') {
        showUserErrorToast(res, failureText)
        return
      }
      const data = res.data || {}
      if (data.already_ready) {
        toast.success(t('toast.texture.encoder_ready', '{name} 已就绪', { name: label }))
        await checkEncoderStatus()
        return
      }
      if (data.download_kind === 'external' && data.url) {
        if (window.pywebview.api.open_external_url) {
          await window.pywebview.api.open_external_url(data.url)
        }
        toast.info(t('toast.texture.encoder_site_opened', '已打开官方页面，安装完成后返回此处即可识别。'))
        await checkEncoderStatus()
        return
      }
      toast.info(t('toast.texture.encoder_download_started', '已启动 {name} 下载任务，请留意底部状态栏。', { name: label }))
      scheduleToolStatusRefresh(0)
    } catch (e) {
      console.error('下载转换档位工具失败:', e)
      showUserErrorToast(e, failureText)
    } finally {
      appStore.isLoading = false
    }
  }

  // 3. 启动分析 (扫描)
  const startAnalysis = async (packageIds, targetScope = 'active', extraOptions = {}) => {
    const hasDirectTarget = !!extraOptions.single_mod_target
    if (!window.pywebview || (!hasDirectTarget && targetScope !== 'all' && packageIds.length === 0)) return
    isAnalyzing.value = true
    lastTargetPackageIds.value = [...packageIds]
    lastTargetScope.value = targetScope
    lastSingleModTargetKey.value = hasDirectTarget ? getSingleTargetKey(extraOptions.single_mod_target) : ''
    currentOptimizationTaskId.value = ''
    if (!hasDirectTarget) {
      modsData.value = []
    }
    try {
      const res = await window.pywebview.api.texture_analyze_mods(packageIds, {
        ...appStore.settings.texture_opt,
        ...extraOptions,
        target_scope: targetScope,
      })
      if (checkResult(res, t('check.texture.start_analysis', '启动贴图分析'), false)) {
        bindTaskId(res.data, 'analyze')
        applyReturnedTaskState(res.data)
        applySnapshotPayload(res.data)
      } else {
        isAnalyzing.value = false
      }
    } catch (e) {
      console.error('启动贴图分析失败:', e)
      showUserErrorToast(e, t('errors.texture.start_analysis_failed', '启动贴图分析失败。请检查模组路径、文件权限和后台日志后重试。'))
      isAnalyzing.value = false
    }
  }

  // 4. 启动优化/清理
  const startOptimization = async (packageIds, action = 'optimize', targetScope = 'active', extraOptions = {}) => {
    const hasDirectTarget = !!extraOptions.single_mod_target
    if (!window.pywebview || (!hasDirectTarget && action !== 'clean_generated' && targetScope !== 'all' && packageIds.length === 0)) return
    isOptimizing.value = true
    lastTargetPackageIds.value = [...packageIds]
    lastTargetScope.value = targetScope
    lastOptimizationAction.value = action
    lastSingleModTargetKey.value = hasDirectTarget ? getSingleTargetKey(extraOptions.single_mod_target) : ''
    currentAnalysisTaskId.value = ''
    try {
      const res = await window.pywebview.api.texture_start_task(packageIds, action, {
        ...appStore.settings.texture_opt,
        ...extraOptions,
        target_scope: targetScope,
      })
      if (checkResult(res, t('check.texture.start_task', '启动优化任务'), false)) {
        bindTaskId(res.data, 'optimize')
        applyReturnedTaskState(res.data)
        applySnapshotPayload(res.data)
      } else {
        isOptimizing.value = false
      }
    } catch (e) {
      console.error('启动贴图任务失败:', e)
      showUserErrorToast(
        e,
        action === 'clean_generated'
          ? t('errors.texture.start_clean_failed', '启动贴图清理失败。请检查模组路径、文件权限和后台日志后重试。')
          : t('errors.texture.start_optimize_failed', '启动贴图生成失败。请检查工具配置、模组路径、文件权限和后台日志后重试。'),
      )
      isOptimizing.value = false
    }
  }

  const cancelCurrentTask = async () => {
    if (!window.pywebview) return
    const task = currentTask.value
    if (!task) return
    try {
      const ok = await appStore.cancelTaskByProgress(task)
      if (ok) {
        markTaskCancelling()
      }
    } catch (e) {
      console.error('取消贴图任务失败:', e)
    }
  }

  const handleDownloadEvent = (payload) => {
    if (!payload || payload.status !== 'success') return
    if (!isTextureToolDownloadPayload(payload)) return
    scheduleToolStatusRefresh(0)
  }

  const applyExclusionsPayload = (payload = {}) => {
    textureExclusions.value = {
      schema_version: Number(payload.schema_version || 1),
      mods: Array.isArray(payload.mods) ? payload.mods.map(item => ({
        package_id: String(item.package_id || ''),
        updated_at: Number(item.updated_at || 0),
      })) : [],
      files: Array.isArray(payload.files) ? payload.files.map(item => ({
        mod_path: String(item.mod_path || ''),
        rel_path: String(item.rel_path || ''),
        updated_at: Number(item.updated_at || 0),
      })) : [],
    }
  }

  const loadResultHistory = async () => {
    if (!window.pywebview) return
    try {
      const res = await window.pywebview.api.texture_get_result_history(3)
      if (!checkResult(res, t('check.texture.load_result_history', '读取贴图结果历史'), false)) return
      resultHistory.value = Array.isArray(res.data) ? res.data.map(normalizeResultHistoryItem) : []
      if (!selectedResultPath.value || !resultHistory.value.some(item => item.result_path === selectedResultPath.value)) {
        selectedResultPath.value = resultHistory.value[0]?.result_path || ''
      }
    } catch (e) {
      console.error('读取贴图结果历史失败:', e)
    }
  }

  const loadExclusions = async () => {
    if (!window.pywebview) return
    try {
      const res = await window.pywebview.api.texture_get_exclusions()
      if (!checkResult(res, t('check.texture.load_exclusions', '读取贴图排除规则'), false)) return
      applyExclusionsPayload(res.data)
    } catch (e) {
      console.error('读取贴图排除规则失败:', e)
    }
  }

  const isModExcluded = (packageId) => {
    const normalized = String(packageId || '').trim().toLowerCase()
    return !!normalized && textureExclusions.value.mods.some(item => String(item.package_id || '').trim().toLowerCase() === normalized)
  }

  const isFileExcluded = (modPath, relPath) => {
    const normalizedModPath = String(modPath || '').trim().toLowerCase()
    const normalizedRelPath = String(relPath || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '').toLowerCase()
    return !!normalizedModPath && !!normalizedRelPath && textureExclusions.value.files.some(item => (
      String(item.mod_path || '').trim().toLowerCase() === normalizedModPath
      && String(item.rel_path || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '').toLowerCase() === normalizedRelPath
    ))
  }

  const toggleModExclusion = async (packageId, exclude) => {
    if (!window.pywebview) return false
    const res = await window.pywebview.api.texture_toggle_mod_exclusion(packageId, !!exclude)
    const actionLabel = exclude ? t('check.texture.exclude_mod', '添加模组排除') : t('check.texture.include_mod', '移除模组排除')
    if (!checkResult(res, actionLabel, false)) return false
    applyExclusionsPayload(res.data)
    return true
  }

  const toggleFileExclusion = async (modPath, relPath, exclude) => {
    if (!window.pywebview) return false
    const res = await window.pywebview.api.texture_toggle_file_exclusion(modPath, relPath, !!exclude)
    const actionLabel = exclude ? t('check.texture.exclude_file', '添加文件排除') : t('check.texture.include_file', '移除文件排除')
    if (!checkResult(res, actionLabel, false)) return false
    applyExclusionsPayload(res.data)
    return true
  }

  // 5. 接收 EventBus 推流 (此函数将在 App.vue 初始化时挂载)
  const handleProgressEvent = (payload) => {
    const { id, type, status, progress, message, metrics } = payload
    const isKnownTask = bindIncomingTaskIfNeeded(payload)

    // 如果不是当前任务，忽略
    if (!isKnownTask) return

    const hasSnapshotPayload =
      (metrics.summary && typeof metrics.summary === 'object')
      || Array.isArray(metrics.final_mods)
      || Array.isArray(metrics.mods)
    if (hasSnapshotPayload) {
      applySnapshotPayload(metrics)
    }

    if (metrics.current_entry) {
      upsertCurrentEntry(metrics.current_entry)
    }

    // 处理分析进度
    if (type === 'texture-opt-analyze') {
      if (status === 'success' || status === 'failed' || status === 'cancelled') {
        lastFinishedProgress.value = buildProgressState(currentTask.value || taskStore.getTask(id))
        isAnalyzing.value = false
        currentAnalysisTaskId.value = ''
        if (!currentOptimizationTaskId.value) {
          currentTaskId.value = ''
        }
        lastSingleModTargetKey.value = ''
      }
    }

    // 处理优化进度
    if (type === 'texture-opt') {
      if (status === 'success' || status === 'failed' || status === 'cancelled') {
        if (status === 'success' && metrics.task_action === 'clean_generated') {
          if (metrics.clean_without_source === true) {
            clearSourcelessOutputsFromSingleRow()
          } else {
            clearGeneratedOutputsFromCurrentRows({ outputFormat: metrics.clean_output_format || 'dds' })
          }
        }
        lastFinishedProgress.value = buildProgressState(currentTask.value || taskStore.getTask(id))
        isOptimizing.value = false
        selectedResultPath.value = String(metrics.result_path || selectedResultPath.value || '')
        loadResultHistory()
        currentOptimizationTaskId.value = ''
        if (!currentAnalysisTaskId.value) {
          currentTaskId.value = ''
        }
        lastSingleModTargetKey.value = ''
        const shouldRefreshAnalyze =
          status === 'success'
          && metrics.refresh_after_analyze === true
          && !isAnalyzing.value
          && lastOptimizationAction.value !== 'optimize'
          && (lastTargetScope.value === 'all' || lastTargetPackageIds.value.length > 0)
        if (shouldRefreshAnalyze) {
          startAnalysis([...lastTargetPackageIds.value], lastTargetScope.value)
        }
      }
    }
  }

  return {
    // 任务状态
    isAnalyzing, isOptimizing, currentTaskId, currentAnalysisTaskId, currentOptimizationTaskId,
    benchmarkRunning, benchmarkMessage, runWorkerBenchmark, saveWorkerOverrides,
    // 数据与视图状态
    modsData, globalSummary, progressState, viewMode, toolStatus, encoderStatus, encoderRows, machinePlan, planSourceLabel,
    resultHistory, textureExclusions, isResultDrawerOpen, selectedResultPath,
    // 任务动作
    checkToolStatus, checkEncoderStatus, downloadEncoder, startAnalysis, startOptimization, cancelCurrentTask, handleProgressEvent, handleDownloadEvent,
    // 结果与排除项
    loadResultHistory, loadExclusions, isModExcluded, isFileExcluded, toggleModExclusion, toggleFileExclusion, getRowKey,
  }
})
