import * as k8s from '@kubernetes/client-node';

export interface PodInfo {
  name: string;
  status: string;
  ip: string;
  node: string;
  ready: boolean;
}

export class KubernetesHelper {
  private coreApi: k8s.CoreV1Api;
  private namespace: string;

  constructor(namespace?: string) {
    const kc = new k8s.KubeConfig();
    kc.loadFromDefault();
    this.coreApi = kc.makeApiClient(k8s.CoreV1Api);
    this.namespace = namespace || process.env.K8S_NAMESPACE || 'elitea-staging';
  }

  async getPods(labelSelector: string): Promise<PodInfo[]> {
    const response = await this.coreApi.listNamespacedPod({
      namespace: this.namespace,
      labelSelector,
    });
    return (response.items || []).map((pod) => ({
      name: pod.metadata?.name || '',
      status: pod.status?.phase || 'Unknown',
      ip: pod.status?.podIP || '',
      node: pod.spec?.nodeName || '',
      ready: pod.status?.containerStatuses?.every((c) => c.ready) ?? false,
    }));
  }

  async getPodNames(labelSelector: string): Promise<string[]> {
    const pods = await this.getPods(labelSelector);
    return pods.map((p) => p.name);
  }

  async getReadyPodCount(labelSelector: string): Promise<number> {
    const pods = await this.getPods(labelSelector);
    return pods.filter((p) => p.ready).length;
  }

  async deletePod(podName: string): Promise<void> {
    await this.coreApi.deleteNamespacedPod({
      name: podName,
      namespace: this.namespace,
      gracePeriodSeconds: 0,
    });
  }

  async waitForPodReady(
    labelSelector: string,
    expectedCount: number,
    timeoutMs: number = 120_000
  ): Promise<void> {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const readyCount = await this.getReadyPodCount(labelSelector);
      if (readyCount >= expectedCount) return;
      await new Promise((r) => setTimeout(r, 2000));
    }
    throw new Error(
      `Timeout: expected ${expectedCount} ready pods for ${labelSelector}`
    );
  }

  async simulatePodRestart(labelSelector: string): Promise<string> {
    const pods = await this.getPodNames(labelSelector);
    if (pods.length === 0) throw new Error(`No pods found for ${labelSelector}`);
    const targetPod = pods[0];
    await this.deletePod(targetPod);
    return targetPod;
  }
}

export function createK8sHelper(namespace?: string): KubernetesHelper {
  return new KubernetesHelper(namespace);
}
