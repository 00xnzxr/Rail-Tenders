import { canAccess, type Surface } from './lib/roles';
import { createBrowserRouter, Navigate } from 'react-router-dom';
import AppLayout from './components/layout/AppLayout';
import LoginPage from './pages/LoginPage';
import DashboardPage from './pages/DashboardPage';
import TendersPage from './pages/TendersPage';
import GemSearchPage from './pages/GemSearchPage';
import TenderDetailPage from './pages/TenderDetailPage';
import ArchivePage from './pages/ArchivePage';
import ChecklistPage from './pages/ChecklistPage';
import AdminReviewsPage from './pages/AdminReviewsPage';
import ScrapeMonitorPage from './pages/ScrapeMonitorPage';
import SettingsPage from './pages/SettingsPage';

import DocumentGeneratorPage from './pages/DocumentGeneratorPage';
import DocumentsListPage from './pages/DocumentsListPage';
import OfflineDocumentsListPage from './pages/OfflineDocumentsListPage';
import OfflineSignPage from './pages/OfflineSignPage';
import SignaturesPage from './pages/SignaturesPage';

// Admin pages
import AdminDashboardPage from './pages/admin/AdminDashboardPage';
import LetterheadPage from './pages/admin/LetterheadPage';
import AgentBuilderPage from './pages/admin/AgentBuilderPage';
import AgentEditorPage from './pages/admin/AgentEditorPage';
import AgentTestingPage from './pages/admin/AgentTestingPage';
import AgentMonitoringPage from './pages/admin/AgentMonitoringPage';
import AgentLibraryPage from './pages/admin/AgentLibraryPage';
import AgentPipelinePage from './pages/admin/AgentPipelinePage';
import AgentMemoryPage from './pages/admin/AgentMemoryPage';
import MCPServersPage from './pages/admin/MCPServersPage';
import AgentChatPage from './pages/AgentChatPage';
import CommandCenterPage from './pages/CommandCenterPage';
import DocumentWorkspaceDetailPage from './pages/DocumentWorkspaceDetailPage';
import AnnexuresWorkspacePage from './pages/AnnexuresWorkspacePage';
import NotificationsPage from './pages/NotificationsPage';
import UserManagementPage from './pages/admin/UserManagementPage';
import PlatformSettingsPage from './pages/admin/PlatformSettingsPage';
import TenderScopePage from './pages/admin/TenderScopePage';
import AdminTenderScoringPage from './pages/admin/AdminTenderScoringPage';
import DataSecurityPage from './pages/admin/DataSecurityPage';
import AuditLogPage from './pages/admin/AuditLogPage';
import TemplatesPage from './pages/admin/TemplatesPage';
import BatchProcessingPage from './pages/admin/BatchProcessingPage';
import ContextManagementPage from './pages/admin/ContextManagementPage';
import TrainingDatasetsPage from './pages/admin/TrainingDatasetsPage';
import UsageBudgetsPage from './pages/admin/UsageBudgetsPage';
import RatecardsPage from './pages/admin/RatecardsPage';
import WorkflowListPage from './pages/admin/WorkflowListPage';
import WorkflowEditorPage from './pages/admin/WorkflowEditorPage';

// Batch Processing - Claude Message Batches API (50% cost discount)
// eslint-disable-next-line
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const token = localStorage.getItem('drpl_token');
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const role = localStorage.getItem('drpl_role');
  if (role !== 'admin' && role !== 'master_admin') return <Navigate to="/" replace />;
  return <>{children}</>;
}

function MasterAdminRoute({ children }: { children: React.ReactNode }) {
  const role = localStorage.getItem('drpl_role');
  if (role !== 'master_admin') return <Navigate to="/" replace />;
  return <>{children}</>;
}

/**
 * Renders only if the user's role reaches `surface`.
 *
 * Cosmetic, like the two guards above: drpl_role lives in localStorage and the
 * user can edit it. Every surface gated here is also gated server-side by
 * `require_surface` — see src/lib/roles.ts.
 */
function RoleRoute({ surface, children }: { surface: Surface; children: React.ReactNode }) {
  const role = localStorage.getItem('drpl_role');
  if (!canAccess(role, surface)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** Universal landing: every role starts on the calm Home daily desk. */
function HomeLanding() {
  return <DashboardPage />;
}

export const router = createBrowserRouter([
  {
    path: '/login',
    element: <LoginPage />,
  },
  {
    path: '/',
    element: (
      <ProtectedRoute>
        <AppLayout />
      </ProtectedRoute>
    ),
    children: [
      { index: true, element: <HomeLanding /> },
      { path: 'tenders', element: <TendersPage /> },
      { path: 'tenders/view/:view', element: <TendersPage /> },
      { path: 'gem-search', element: <GemSearchPage /> },
      { path: 'archive', element: <ArchivePage /> },
      { path: 'tenders/:id', element: <TenderDetailPage /> },
      { path: 'tenders/:id/checklist', element: <ChecklistPage /> },
      { path: 'tenders/:id/workspace/annexures', element: <AnnexuresWorkspacePage /> },
      { path: 'tenders/:id/workspace/:itemId', element: <DocumentWorkspaceDetailPage /> },
      { path: 'tenders/:id/command-center', element: <CommandCenterPage /> },
      { path: 'command-center', element: <CommandCenterPage /> },
      { path: 'command-center/:sessionId', element: <CommandCenterPage /> },
      { path: 'notifications', element: <NotificationsPage /> },
      // Documents tab is now the offline-PDF "Upload & Sign" experience.
      { path: 'documents', element: <RoleRoute surface="documents"><OfflineDocumentsListPage /></RoleRoute> },
      { path: 'documents/sign/:id', element: <RoleRoute surface="documents"><OfflineSignPage /></RoleRoute> },
      // The letterhead/TipTap generator is kept as a secondary surface.
      { path: 'documents/generate', element: <RoleRoute surface="document_generator"><DocumentGeneratorPage /></RoleRoute> },
      { path: 'documents/generate/:id', element: <RoleRoute surface="document_generator"><DocumentGeneratorPage /></RoleRoute> },
      // Backward-compat: the old standalone-documents library and editor.
      { path: 'documents/library', element: <RoleRoute surface="document_generator"><DocumentsListPage /></RoleRoute> },
      { path: 'documents/library/:id', element: <RoleRoute surface="document_generator"><DocumentGeneratorPage /></RoleRoute> },
      { path: 'signatures', element: <RoleRoute surface="signatures"><SignaturesPage /></RoleRoute> },
      { path: 'reviews', element: <AdminRoute><AdminReviewsPage /></AdminRoute> },
      { path: 'scrape-monitor', element: <MasterAdminRoute><ScrapeMonitorPage /></MasterAdminRoute> },
      { path: 'settings', element: <SettingsPage /> },
      // Master Admin routes — protected by role guard
      { path: 'admin', element: <MasterAdminRoute><AdminDashboardPage /></MasterAdminRoute> },
      { path: 'admin/users', element: <MasterAdminRoute><UserManagementPage /></MasterAdminRoute> },
      { path: 'admin/usage', element: <MasterAdminRoute><UsageBudgetsPage /></MasterAdminRoute> },
      { path: 'admin/settings', element: <MasterAdminRoute><PlatformSettingsPage /></MasterAdminRoute> },
      { path: 'admin/tender-scope', element: <MasterAdminRoute><TenderScopePage /></MasterAdminRoute> },
      { path: 'admin/tender-scoring', element: <MasterAdminRoute><AdminTenderScoringPage /></MasterAdminRoute> },
      { path: 'admin/agents', element: <Navigate to="/admin/agent-builder" replace /> },
      { path: 'admin/security', element: <MasterAdminRoute><DataSecurityPage /></MasterAdminRoute> },
      { path: 'admin/audit', element: <MasterAdminRoute><AuditLogPage /></MasterAdminRoute> },
      { path: 'admin/templates', element: <MasterAdminRoute><TemplatesPage /></MasterAdminRoute> },
      { path: 'admin/letterheads', element: <MasterAdminRoute><LetterheadPage /></MasterAdminRoute> },
      { path: 'admin/agent-builder', element: <MasterAdminRoute><AgentBuilderPage /></MasterAdminRoute> },
      { path: 'admin/agent-builder/new', element: <MasterAdminRoute><AgentEditorPage /></MasterAdminRoute> },
      { path: 'admin/agent-builder/:id', element: <MasterAdminRoute><AgentEditorPage /></MasterAdminRoute> },
      { path: 'admin/agent-builder/:id/test', element: <MasterAdminRoute><AgentTestingPage /></MasterAdminRoute> },
      { path: 'admin/agent-builder/:id/monitor', element: <MasterAdminRoute><AgentMonitoringPage /></MasterAdminRoute> },
      { path: 'admin/agent-library', element: <MasterAdminRoute><AgentLibraryPage /></MasterAdminRoute> },
      { path: 'admin/agent-pipeline', element: <MasterAdminRoute><AgentPipelinePage /></MasterAdminRoute> },
      { path: 'admin/agent-memory', element: <MasterAdminRoute><AgentMemoryPage /></MasterAdminRoute> },
      { path: 'admin/mcp-servers', element: <MasterAdminRoute><MCPServersPage /></MasterAdminRoute> },
      { path: 'admin/batch-processing', element: <MasterAdminRoute><BatchProcessingPage /></MasterAdminRoute> },
      { path: 'admin/context-management', element: <MasterAdminRoute><ContextManagementPage /></MasterAdminRoute> },
      { path: 'admin/training-datasets', element: <MasterAdminRoute><TrainingDatasetsPage /></MasterAdminRoute> },
      { path: 'admin/ratecards', element: <MasterAdminRoute><RatecardsPage /></MasterAdminRoute> },
      { path: 'admin/workflows', element: <MasterAdminRoute><WorkflowListPage /></MasterAdminRoute> },
      { path: 'admin/workflows/:id', element: <MasterAdminRoute><WorkflowEditorPage /></MasterAdminRoute> },
      { path: 'agent-chat/:agentKey', element: <MasterAdminRoute><AgentChatPage /></MasterAdminRoute> },
      { path: 'agent-chat/:agentKey/:sessionId', element: <MasterAdminRoute><AgentChatPage /></MasterAdminRoute> },
    ],
  },
]);
