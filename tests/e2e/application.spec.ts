import { expect, test } from "@playwright/test";
import type { APIRequestContext } from "@playwright/test";

const bootstrapToken = process.env.SPA_E2E_BOOTSTRAP_TOKEN ?? "spa-e2e-bootstrap-token";

async function postJson(request: APIRequestContext, path: string, data: unknown) {
  const response = await request.post(path, { data });
  expect(response.status(), `${path}: ${await response.text()}`).toBeLessThan(400);
  return response.json();
}

async function awaitJob(request: APIRequestContext, jobId: string) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const response = await request.get(`/api/v1/jobs/${jobId}`);
    expect(response.ok()).toBeTruthy();
    const job = await response.json();
    if (["completed", "failed", "cancelled", "interrupted", "recoverable"].includes(job.state)) {
      expect(job.state, JSON.stringify(job.error ?? job)).toBe("completed");
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 75));
  }
  throw new Error(`Timed out waiting for job ${jobId}`);
}

test("one-time bootstrap establishes the local run session", async ({ page }) => {
  const response = await page.goto(`/bootstrap?token=${encodeURIComponent(bootstrapToken)}`);
  expect(response?.ok()).toBeTruthy();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole("heading", { name: /projects/i })).toBeVisible();

  const health = await page.request.get("/api/v1/health/ready");
  expect(health.ok()).toBeTruthy();
  expect((await health.json()).status).toMatch(/ready|ok/);
});

test("project creation reaches Camera Setup through the production UI", async ({ page }) => {
  await page.goto(`/bootstrap?token=${encodeURIComponent(bootstrapToken)}`);
  const projectName = `E2E replay ${Date.now()}`;
  const created = await page.request.post("/api/v1/projects", {
    data: { name: projectName },
    headers: { "Idempotency-Key": `e2e-${Date.now()}` },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();

  await page.goto(`/projects/${project.project_id}/camera`);
  await expect(page.getByRole("heading", { name: /camera setup/i })).toBeVisible();
  await expect(page.getByText(/intrinsics supplied per frame/i)).toBeVisible();
});

test("invalid bootstrap credentials do not grant a run session", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const response = await page.goto("/bootstrap?token=invalid-token");
  expect(response?.status()).toBeGreaterThanOrEqual(400);
  const projects = await page.request.get("/api/v1/projects");
  expect(projects.status()).toBeGreaterThanOrEqual(400);
  await context.close();
});

test("replay atlas reaches review and a checksummed export", async ({ page }) => {
  await page.goto(`/bootstrap?token=${encodeURIComponent(bootstrapToken)}`);
  const request = page.request;
  const project = await postJson(request, "/api/v1/projects", { name: "Browser replay atlas" });
  const projectId = project.project_id;

  await postJson(request, "/api/v1/camera/connect", {
    project_id: projectId,
    adapter: "replay",
    device_id: "replay:synthetic",
  });
  await page.goto(`/projects/${projectId}/camera`);
  await expect(page.getByRole("heading", { name: "Camera Setup" })).toBeVisible();
  await expect(page.getByText(/Intrinsics supplied per frame/i)).toBeVisible();

  const capture = await postJson(request, `/api/v1/projects/${projectId}/capture-sets`, {
    name: "Browser replay capture",
    source: "replay",
  });
  const captured = await postJson(
    request,
    `/api/v1/projects/${projectId}/capture-sets/${capture.capture_set_id}/frames:capture`,
    { count: 3 },
  );
  const mapping = await postJson(request, `/api/v1/projects/${projectId}/maps`, {
    capture_set_id: capture.capture_set_id,
    capture_set_revision: captured.capture_set.revision,
    compute_profile: "auto",
    name: "Browser replay map",
  });
  expect(mapping.effective_compute_profile).toBe("depth_assisted_replay_v1");
  await awaitJob(request, mapping.job_id);
  await postJson(request, `/api/v1/projects/${projectId}/maps/${mapping.map_id}/activate`, {});
  await page.goto(`/projects/${projectId}/mapping`);
  await expect(page.getByRole("heading", { name: "Scene Capture & Mapping" })).toBeVisible();
  await expect(page.getByText("Browser replay map").first()).toBeVisible();

  const probeCapture = await postJson(request, `/api/v1/projects/${projectId}/probe-captures`, {
    name: "Browser replay probe",
    source: "replay",
  });
  await postJson(request, `/api/v1/projects/${projectId}/probe-captures/${probeCapture.id}/frames:capture`, {
    count: 3,
  });
  const probe = await postJson(request, `/api/v1/projects/${projectId}/probe-calibrations`, {
    probe_capture_id: probeCapture.id,
    name: "Browser probe v1",
    activate: true,
  });
  const registration = await postJson(request, `/api/v1/projects/${projectId}/registrations`, {
    name: "Browser tissue registration",
    map_id: mapping.map_id,
    probe_calibration_id: probe.probe_calibration_id,
  });
  for (let index = 0; index < 3; index += 1) {
    await postJson(
      request,
      `/api/v1/projects/${projectId}/registrations/${registration.registration_id}/observations`,
      { source: "current_frame", label: `browser-view-${index}` },
    );
  }
  await postJson(request, `/api/v1/projects/${projectId}/registrations/${registration.registration_id}/solve`, {});
  await postJson(request, `/api/v1/projects/${projectId}/registrations/${registration.registration_id}/validate`, {});
  await postJson(request, `/api/v1/projects/${projectId}/registrations/${registration.registration_id}/activate`, {});
  await page.goto(`/projects/${projectId}/registration`);
  await expect(page.getByRole("heading", { name: "Probe & Registration" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Can’t track the probe?" })).toBeVisible();

  const session = await postJson(request, `/api/v1/projects/${projectId}/sessions`, {
    name: "Browser replay session",
    notes: "browser E2E",
    compute_profile: "cpu",
  });
  await postJson(request, `/api/v1/projects/${projectId}/sessions/${session.session_id}/start`, {});
  await postJson(request, `/api/v1/projects/${projectId}/sessions/${session.session_id}/painted-points`, {
    command_id: "browser-point-1",
  });
  await postJson(request, `/api/v1/projects/${projectId}/sessions/${session.session_id}/stop`, {});
  await postJson(request, `/api/v1/projects/${projectId}/sessions/${session.session_id}/finalize`, {});

  await page.goto(`/projects/${projectId}/sessions/${session.session_id}/review`);
  await expect(page.getByRole("heading", { name: "Browser replay session" })).toBeVisible();
  await expect(page.getByText("Persisted records")).toBeVisible();
  await expect(page.getByText("point", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Export session" }).click();
  await page.getByLabel("CSV tables").check();
  await page.getByRole("button", { name: "Create export job" }).click();
  await expect(page.getByText("Export job created")).toBeVisible();

  const exportsResponse = await request.get(`/api/v1/projects/${projectId}/sessions/${session.session_id}/exports`);
  expect(exportsResponse.ok()).toBeTruthy();
  const exports = await exportsResponse.json();
  expect(exports).toHaveLength(1);
  await awaitJob(request, exports[0].job_id);
  await page.reload();
  await expect(page.getByText("csv", { exact: true }).first()).toBeVisible();
  const completedResponse = await request.get(`/api/v1/projects/${projectId}/sessions/${session.session_id}/exports`);
  const completed = (await completedResponse.json())[0];
  expect(completed.checksum_sha256).toMatch(/^[a-f0-9]{64}$/);
  const download = await request.get(completed.download_url);
  expect(download.ok()).toBeTruthy();
  expect((await download.text()).split("\n")[0]).toContain("record_type");

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings & Diagnostics" })).toBeVisible();
});
