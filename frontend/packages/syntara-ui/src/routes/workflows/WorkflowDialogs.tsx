import { FormGroup, HelperText, HelperTextItem, Stack, StackItem } from '@patternfly/react-core'
import type { WorkflowAPI } from '@syntara/contracts'
import { useCallback, useState } from 'react'

import { SynConfirmationDialog } from '../../components/dialogs/SynConfirmationDialog'
import type { DialogState } from '../../hooks/useDialogState'
import { useAlerts } from '../../providers/alerts'
import { detachPromise } from '../../utils/detachPromise'
import type { ProjectRead } from '../access/types'
import { ProjectFormModal } from '../access-management/ProjectFormModal'
import { ProjectDeleteDialog } from '../access-management/projects/ProjectDeleteDialog'
import type { RuntimeEngine } from '../builder/components/runtimeEngine'
import { RuntimeEngineSelect } from '../builder/components/RuntimeEngineSelect'
import { RunWorkflowModal } from '../builder/components/RunWorkflowModal'
import { ServiceAccountSelect } from '../builder/node-forms/ServiceAccountSelect'
import { PublishWorkflowDialog } from '../builder/PublishWorkflowDialog'
import { hasNonEmptyInputSchema } from '../builder/utils/triggerReferenceCheck'

import { ImportWorkflowDialog } from './ImportWorkflowDialog'
import { resolveWorkflowRunTrigger, type WorkflowRunTrigger } from './resolveWorkflowRunTrigger'
import { WorkflowDeleteDialog } from './WorkflowDeleteDialog'

type Workflow = WorkflowAPI.components['schemas']['WorkflowRead']

type PendingRunInput = {
  workflow: Workflow
  trigger: WorkflowRunTrigger
  serviceAccountId: string | null
  runtimeEngine: RuntimeEngine | undefined
}

type WorkflowRunOptions = {
  serviceAccountId?: string
  runtimeEngine?: RuntimeEngine
}

function RunModeField({
  value,
  onChange,
  enabled,
}: Readonly<{
  value: RuntimeEngine | undefined
  onChange: (value: RuntimeEngine | undefined) => void
  enabled: boolean
}>) {
  return (
    <FormGroup label="Agent runtime" fieldId="workflow-runtime-engine">
      <RuntimeEngineSelect
        id="workflow-runtime-engine"
        value={value}
        onChange={onChange}
        isDisabled={!enabled}
        ariaLabel="Agent runtime"
      />
      <HelperText>
        <HelperTextItem>Overrides the deployment default for agent steps in this workflow run.</HelperTextItem>
      </HelperText>
    </FormGroup>
  )
}

function RunAsServiceAccountField({
  selectedId,
  onChange,
  projectId,
  enabled,
}: Readonly<{
  selectedId: string | null
  onChange: (id: string | null) => void
  projectId?: string | null
  enabled: boolean
}>) {
  return (
    <FormGroup label="Run as service account (optional)" fieldId="workflow-runtime-service-account">
      <ServiceAccountSelect
        id="workflow-runtime-service-account"
        selectedIds={selectedId ? [selectedId] : []}
        onChange={(ids) => onChange(ids[0] ?? null)}
        projectId={projectId}
        selectionMode="single"
        enabled={enabled && Boolean(projectId)}
        allowCreate={false}
      />
      <HelperText>
        <HelperTextItem>
          Required for sandboxed agent steps; the human requester remains the audit owner.
        </HelperTextItem>
      </HelperText>
    </FormGroup>
  )
}

function WorkflowRunConfirmation({
  isOpen,
  onClose,
  onConfirm,
  confirmLoading,
  workflowName,
  runtimeEngine,
  onRuntimeEngineChange,
  serviceAccountId,
  onServiceAccountChange,
  projectId,
}: Readonly<{
  isOpen: boolean
  onClose: () => void
  onConfirm: () => void
  confirmLoading: boolean
  workflowName?: string
  runtimeEngine: RuntimeEngine | undefined
  onRuntimeEngineChange: (value: RuntimeEngine | undefined) => void
  serviceAccountId: string | null
  onServiceAccountChange: (value: string | null) => void
  projectId?: string | null
}>) {
  return (
    <SynConfirmationDialog
      isOpen={isOpen}
      onClose={onClose}
      onConfirm={onConfirm}
      title={`Run ${workflowName}?`}
      confirmLabel="Run now"
      confirmLoading={confirmLoading}
    >
      <Stack hasGutter>
        <StackItem>
          You are about to manually run this workflow. This action will start the workflow immediately, bypassing its
          normal trigger conditions.
        </StackItem>
        <StackItem>
          <RunModeField value={runtimeEngine} onChange={onRuntimeEngineChange} enabled={isOpen} />
        </StackItem>
        <StackItem>
          <RunAsServiceAccountField
            selectedId={serviceAccountId}
            onChange={onServiceAccountChange}
            projectId={projectId}
            enabled={isOpen && runtimeEngine !== 'in_process'}
          />
        </StackItem>
      </Stack>
    </SynConfirmationDialog>
  )
}

/**
 * Props for WorkflowDialogs component
 */
type WorkflowDialogsProps = {
  /** Dialog state for workflow run confirmation */
  runDialog: DialogState<Workflow>
  /** Dialog state for workflow deletion confirmation */
  deleteDialog: DialogState<Workflow>
  /** Dialog state for workflow publish modal */
  publishDialog: DialogState<Workflow>
  /** Dialog state for workflow unpublish confirmation */
  unpublishDialog: DialogState<Workflow>
  /** Controls visibility of the import workflow dialog */
  importDialogOpen: boolean
  /** Callback to toggle import dialog visibility */
  setImportDialogOpen: (open: boolean) => void
  /** Dialog state for project edit modal */
  projectEditDialog: DialogState<ProjectRead>
  /** Dialog state for project deletion confirmation */
  projectDeleteDialog: DialogState<ProjectRead>
  /** Handler to execute a workflow with optional trigger inputs */
  onRunWorkflow: (
    workflow: Workflow,
    inputData?: Record<string, unknown>,
    triggerNodeId?: string,
    runOptions?: WorkflowRunOptions
  ) => void
  /** Handler to delete a workflow - dialog closes in onSettled callback */
  onDeleteWorkflow: (workflow: Workflow) => void
  /** Handler to publish a workflow - dialog closes in onSettled callback */
  onPublishWorkflow: (workflow: Workflow, publishName?: string, description?: string) => void
  /** Handler to unpublish a workflow - dialog closes in onSettled callback */
  onUnpublishWorkflow: (workflow: Workflow) => void
  /** Handler to delete a project - dialog closes in onSettled callback */
  onDeleteProject: (project: ProjectRead) => void
  /** Callback to refetch workflows list after import success */
  onRefetchWorkflows: () => void
  /** Callback to refetch projects list after project edit success */
  onRefetchProjects: () => void
  /** Loading state for workflow deletion mutation */
  isDeleting: boolean
  /** Loading state for workflow publish mutation */
  isPublishing: boolean
  /** Loading state for project deletion mutation */
  isDeletingProject: boolean
}

export function WorkflowDialogs({
  runDialog,
  deleteDialog,
  publishDialog,
  unpublishDialog,
  importDialogOpen,
  setImportDialogOpen,
  projectEditDialog,
  projectDeleteDialog,
  onRunWorkflow,
  onDeleteWorkflow,
  onPublishWorkflow,
  onUnpublishWorkflow,
  onDeleteProject,
  onRefetchWorkflows,
  onRefetchProjects,
  isDeleting,
  isPublishing,
  isDeletingProject,
}: WorkflowDialogsProps) {
  const { showError } = useAlerts()
  const [isResolvingRun, setIsResolvingRun] = useState(false)
  const [pendingRunInput, setPendingRunInput] = useState<PendingRunInput | null>(null)
  const [runtimeServiceAccountId, setRuntimeServiceAccountId] = useState<string | null>(null)
  const [runtimeEngine, setRuntimeEngine] = useState<RuntimeEngine | undefined>()

  const closeRunInput = useCallback(() => {
    setPendingRunInput(null)
  }, [])

  const closeRunDialog = useCallback(() => {
    setRuntimeServiceAccountId(null)
    setRuntimeEngine(undefined)
    runDialog.close()
  }, [runDialog])
  const workflowToRun = runDialog.item

  const runWithOptionalServiceAccount = useCallback(
    (
      workflow: Workflow,
      inputData: Record<string, unknown>,
      triggerNodeId: string | undefined,
      serviceAccountId: string | null,
      selectedRuntimeEngine: RuntimeEngine | undefined
    ) => {
      const runOptions: WorkflowRunOptions = {}
      if (serviceAccountId) runOptions.serviceAccountId = serviceAccountId
      if (selectedRuntimeEngine) runOptions.runtimeEngine = selectedRuntimeEngine
      onRunWorkflow(workflow, inputData, triggerNodeId, Object.keys(runOptions).length > 0 ? runOptions : undefined)
    },
    [onRunWorkflow]
  )

  const handleConfirmRun = useCallback(() => {
    if (!workflowToRun) return

    setIsResolvingRun(true)
    detachPromise(
      resolveWorkflowRunTrigger(workflowToRun)
        .then((trigger) => {
          if (!trigger) {
            showError({ title: 'Cannot run workflow', description: 'Workflow has no triggers configured' })
            closeRunDialog()
            return
          }

          if (!trigger.hasTriggerReferences && !hasNonEmptyInputSchema(trigger.inputSchema)) {
            runWithOptionalServiceAccount(
              workflowToRun,
              {},
              trigger.triggerNodeId,
              runtimeServiceAccountId,
              runtimeEngine
            )
            closeRunDialog()
            return
          }

          setPendingRunInput({
            workflow: workflowToRun,
            trigger,
            serviceAccountId: runtimeServiceAccountId,
            runtimeEngine,
          })
          closeRunDialog()
        })
        .catch((error: unknown) => {
          showError({
            title: 'Cannot run workflow',
            description: error instanceof Error ? error.message : 'Failed to load workflow trigger details',
          })
          closeRunDialog()
        })
        .finally(() => {
          setIsResolvingRun(false)
        })
    )
  }, [closeRunDialog, runWithOptionalServiceAccount, runtimeEngine, runtimeServiceAccountId, showError, workflowToRun])

  const handleRunWithInputs = useCallback(
    (inputData: Record<string, unknown>, triggerNodeId?: string) => {
      if (!pendingRunInput) return
      runWithOptionalServiceAccount(
        pendingRunInput.workflow,
        inputData,
        triggerNodeId ?? pendingRunInput.trigger.triggerNodeId,
        pendingRunInput.serviceAccountId,
        pendingRunInput.runtimeEngine
      )
      setPendingRunInput(null)
    },
    [pendingRunInput, runWithOptionalServiceAccount]
  )

  return (
    <>
      <WorkflowRunConfirmation
        isOpen={runDialog.isOpen}
        onClose={closeRunDialog}
        onConfirm={handleConfirmRun}
        confirmLoading={isResolvingRun}
        workflowName={workflowToRun?.name}
        runtimeEngine={runtimeEngine}
        onRuntimeEngineChange={(value) => {
          setRuntimeEngine(value)
          if (value === 'in_process') setRuntimeServiceAccountId(null)
        }}
        serviceAccountId={runtimeServiceAccountId}
        onServiceAccountChange={setRuntimeServiceAccountId}
        projectId={workflowToRun?.project_id}
      />

      <RunWorkflowModal
        key={pendingRunInput ? `open-${pendingRunInput.trigger.triggerNodeId}` : 'closed'}
        isOpen={pendingRunInput != null}
        onClose={closeRunInput}
        onConfirm={handleRunWithInputs}
        workflowName={pendingRunInput?.workflow.name ?? ''}
        triggerName={pendingRunInput?.trigger.triggerName ?? 'Trigger'}
        triggerNodeId={pendingRunInput?.trigger.triggerNodeId}
        inputSchema={pendingRunInput?.trigger.inputSchema}
        workflowId={pendingRunInput?.workflow.id}
      />

      <WorkflowDeleteDialog
        isOpen={deleteDialog.isOpen}
        workflowName={deleteDialog.item?.name ?? ''}
        onClose={deleteDialog.close}
        onConfirm={() => {
          if (deleteDialog.item) {
            onDeleteWorkflow(deleteDialog.item)
          }
          // Dialog closes in onSettled callback passed to useWorkflowActions
        }}
        confirmLoading={isDeleting}
      />

      <ImportWorkflowDialog
        isOpen={importDialogOpen}
        onClose={() => setImportDialogOpen(false)}
        onSuccess={onRefetchWorkflows}
      />

      <PublishWorkflowDialog
        isOpen={publishDialog.isOpen}
        isPublishing={isPublishing}
        onClose={publishDialog.close}
        onPublish={(publishName, description) => {
          if (publishDialog.item) {
            onPublishWorkflow(publishDialog.item, publishName, description)
          }
        }}
      />

      <SynConfirmationDialog
        isOpen={unpublishDialog.isOpen}
        onClose={unpublishDialog.close}
        onConfirm={() => {
          if (unpublishDialog.item) {
            onUnpublishWorkflow(unpublishDialog.item)
          }
          // Dialog closes in onSettled callback passed to useWorkflowActions
        }}
        title="Unpublish workflow?"
        confirmLabel="Unpublish"
        confirmVariant="danger"
        titleIconVariant="warning"
      >
        The workflow <strong>{unpublishDialog.item?.name}</strong> will be unpublished. It will no longer be available
        for execution until published again.
      </SynConfirmationDialog>

      <ProjectFormModal
        project={projectEditDialog.item}
        isOpen={projectEditDialog.isOpen}
        onClose={projectEditDialog.close}
        onSuccess={() => {
          onRefetchWorkflows()
          onRefetchProjects()
        }}
      />

      <ProjectDeleteDialog
        projectName={projectDeleteDialog.item?.name}
        isOpen={projectDeleteDialog.isOpen}
        onClose={projectDeleteDialog.close}
        onConfirm={() => {
          if (projectDeleteDialog.item) {
            onDeleteProject(projectDeleteDialog.item)
          }
        }}
        confirmLoading={isDeletingProject}
      />
    </>
  )
}
