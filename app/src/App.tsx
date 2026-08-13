import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "./lib/auth";
import { Loading } from "./components/ui";
import Layout from "./components/Layout";

import Login from "./pages/Login";
import Signup from "./pages/Signup";
import VerifyEmail from "./pages/VerifyEmail";
import ForgotPassword from "./pages/ForgotPassword";
import ResetPassword from "./pages/ResetPassword";
import Projects from "./pages/Projects";
import NewProject from "./pages/NewProject";
import ProjectDetail from "./pages/ProjectDetail";
import GlobalMemory from "./pages/GlobalMemory";
import Programs from "./pages/Programs";
import ProgramsDoc from "./pages/ProgramsDoc";
import ProgramInstance from "./pages/ProgramInstance";
import Billing from "./pages/Billing";
import Tokens from "./pages/Tokens";
import AccountSettings from "./pages/AccountSettings";
import AdminOverview from "./pages/admin/AdminOverview";
import AdminUsers from "./pages/admin/AdminUsers";
import AdminSettings from "./pages/admin/AdminSettings";
import AdminPrograms from "./pages/admin/AdminPrograms";
import AdminProgramDetail from "./pages/admin/AdminProgramDetail";
import KnowledgeBases from "./pages/admin/KnowledgeBases";
import Tools from "./pages/admin/Tools";
import ModelEndpoints from "./pages/admin/ModelEndpoints";

function RequireAuth({ children }: { children: ReactNode }) {
  const { me, loading } = useAuth();
  const location = useLocation();
  if (loading) return <Loading />;
  if (!me) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: ReactNode }) {
  const { isAdmin, loading } = useAuth();
  if (loading) return <Loading />;
  if (!isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />
      <Route path="/verify-email" element={<VerifyEmail />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />

      {/* Landing shortcut: straight into the wizard with the AI path preselected */}
      <Route path="/ai" element={<Navigate to="/projects/new?kind=ai" replace />} />

      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Projects />} />
        <Route path="/projects/new" element={<NewProject />} />
        <Route path="/projects/:id" element={<ProjectDetail />} />
        <Route path="/projects/:id/:tab" element={<ProjectDetail />} />
        <Route path="/projects/:id/:tab/:sub" element={<ProjectDetail />} />
        <Route path="/memory" element={<GlobalMemory />} />
        <Route path="/programs" element={<Programs />} />
        <Route path="/programs/docs" element={<ProgramsDoc />} />
        <Route path="/programs/instances/:id" element={<ProgramInstance />} />
        <Route path="/programs/instances/:id/:tab" element={<ProgramInstance />} />
        <Route path="/programs/instances/:id/:tab/:sub" element={<ProgramInstance />} />
        <Route path="/billing" element={<Billing />} />
        <Route path="/settings/account" element={<AccountSettings />} />
        <Route path="/settings/tokens" element={<Tokens />} />
        <Route
          path="/admin"
          element={
            <RequireAdmin>
              <AdminOverview />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/users"
          element={
            <RequireAdmin>
              <AdminUsers />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/settings"
          element={
            <RequireAdmin>
              <AdminSettings />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/tools"
          element={
            <RequireAdmin>
              <Tools />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/knowledge-bases"
          element={
            <RequireAdmin>
              <KnowledgeBases />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/model-endpoints"
          element={
            <RequireAdmin>
              <ModelEndpoints />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/programs"
          element={
            <RequireAdmin>
              <AdminPrograms />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/programs/:id"
          element={
            <RequireAdmin>
              <AdminProgramDetail />
            </RequireAdmin>
          }
        />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
