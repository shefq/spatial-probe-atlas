import { Suspense, lazy } from "react";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router-dom";
import { ProjectLayout, RootLayout } from "./layouts";
import { Skeleton } from "../components/ui";

const ProjectsPage = lazy(() => import("../pages/ProjectsPage").then((value) => ({ default: value.ProjectsPage })));
const CameraSetupPage = lazy(() => import("../pages/CameraSetupPage").then((value) => ({ default: value.CameraSetupPage })));
const MappingPage = lazy(() => import("../pages/MappingPage").then((value) => ({ default: value.MappingPage })));
const ProbeRegistrationPage = lazy(() => import("../pages/ProbeRegistrationPage").then((value) => ({ default: value.ProbeRegistrationPage })));
const LivePaintingPage = lazy(() => import("../pages/LivePaintingPage").then((value) => ({ default: value.LivePaintingPage })));
const SessionReviewPage = lazy(() => import("../pages/SessionReviewPage").then((value) => ({ default: value.SessionReviewPage })));
const SettingsDiagnosticsPage = lazy(() => import("../pages/SettingsDiagnosticsPage").then((value) => ({ default: value.SettingsDiagnosticsPage })));
const ProbeDesignerPage = lazy(() => import("../pages/ProbeDesignerPage").then((value) => ({ default: value.ProbeDesignerPage })));

const page = (element: React.ReactNode) => <Suspense fallback={<main className="project-loading"><Skeleton lines={7} /></main>}>{element}</Suspense>;
const router = createBrowserRouter([{ element: <RootLayout />, children: [
  { index: true, element: <Navigate to="/projects" replace /> },
  { path: "projects", element: page(<ProjectsPage />) },
  { path: "probe-designer", element: page(<ProbeDesignerPage />) },
  { path: "projects/:projectId", element: <ProjectLayout />, children: [
    { index: true, element: <Navigate to="camera" replace /> },
    { path: "camera", element: page(<CameraSetupPage />) },
    { path: "mapping", element: page(<MappingPage />) },
    { path: "registration", element: page(<ProbeRegistrationPage />) },
    { path: "live", element: page(<LivePaintingPage />) },
    { path: "sessions/:sessionId/review", element: page(<SessionReviewPage />) },
  ] },
  { path: "settings", element: page(<SettingsDiagnosticsPage />) },
  { path: "*", element: <Navigate to="/projects" replace /> },
] }]);

export function App() { return <RouterProvider router={router} />; }
