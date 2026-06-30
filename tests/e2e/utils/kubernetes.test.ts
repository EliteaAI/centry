import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@kubernetes/client-node', () => {
  const mockCoreApi = {
    listNamespacedPod: vi.fn(),
    deleteNamespacedPod: vi.fn(),
  };
  return {
    KubeConfig: vi.fn().mockImplementation(() => ({
      loadFromDefault: vi.fn(),
      makeApiClient: vi.fn().mockReturnValue(mockCoreApi),
    })),
    CoreV1Api: vi.fn(),
    __mockCoreApi: mockCoreApi,
  };
});

import * as k8s from '@kubernetes/client-node';
import { KubernetesHelper, createK8sHelper } from './kubernetes';

const mockCoreApi = (k8s as unknown as { __mockCoreApi: any }).__mockCoreApi;

describe('KubernetesHelper', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('constructor', () => {
    it('uses provided namespace', () => {
      const helper = new KubernetesHelper('custom-ns');
      expect(helper).toBeDefined();
    });

    it('uses K8S_NAMESPACE env var when no namespace provided', () => {
      process.env.K8S_NAMESPACE = 'env-ns';
      const helper = new KubernetesHelper();
      expect(helper).toBeDefined();
      delete process.env.K8S_NAMESPACE;
    });

    it('defaults to elitea-staging', () => {
      delete process.env.K8S_NAMESPACE;
      const helper = new KubernetesHelper();
      expect(helper).toBeDefined();
    });
  });

  describe('getPods', () => {
    it('returns mapped pod info', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          {
            metadata: { name: 'pod-1' },
            status: {
              phase: 'Running',
              podIP: '10.0.0.1',
              containerStatuses: [{ ready: true }],
            },
            spec: { nodeName: 'node-1' },
          },
          {
            metadata: { name: 'pod-2' },
            status: {
              phase: 'Pending',
              podIP: '10.0.0.2',
              containerStatuses: [{ ready: false }],
            },
            spec: { nodeName: 'node-2' },
          },
        ],
      });

      const helper = new KubernetesHelper('test-ns');
      const pods = await helper.getPods('app=pylon-main');

      expect(pods).toHaveLength(2);
      expect(pods[0]).toEqual({
        name: 'pod-1',
        status: 'Running',
        ip: '10.0.0.1',
        node: 'node-1',
        ready: true,
      });
      expect(pods[1]).toEqual({
        name: 'pod-2',
        status: 'Pending',
        ip: '10.0.0.2',
        node: 'node-2',
        ready: false,
      });
    });

    it('handles pods with missing fields', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          {
            metadata: {},
            status: {},
            spec: {},
          },
        ],
      });

      const helper = new KubernetesHelper('test-ns');
      const pods = await helper.getPods('app=test');

      expect(pods[0]).toEqual({
        name: '',
        status: 'Unknown',
        ip: '',
        node: '',
        ready: false,
      });
    });

    it('handles empty pod list', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({ items: [] });
      const helper = new KubernetesHelper('test-ns');
      const pods = await helper.getPods('app=nothing');
      expect(pods).toEqual([]);
    });

    it('handles undefined items', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({});
      const helper = new KubernetesHelper('test-ns');
      const pods = await helper.getPods('app=nothing');
      expect(pods).toEqual([]);
    });
  });

  describe('getPodNames', () => {
    it('returns only pod names', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          { metadata: { name: 'pod-a' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
          { metadata: { name: 'pod-b' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
        ],
      });

      const helper = new KubernetesHelper('test-ns');
      const names = await helper.getPodNames('app=test');
      expect(names).toEqual(['pod-a', 'pod-b']);
    });
  });

  describe('getReadyPodCount', () => {
    it('counts only ready pods', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          { metadata: { name: 'p1' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
          { metadata: { name: 'p2' }, status: { phase: 'Running', containerStatuses: [{ ready: false }] }, spec: {} },
          { metadata: { name: 'p3' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
        ],
      });

      const helper = new KubernetesHelper('test-ns');
      const count = await helper.getReadyPodCount('app=test');
      expect(count).toBe(2);
    });
  });

  describe('deletePod', () => {
    it('calls deleteNamespacedPod with correct params', async () => {
      mockCoreApi.deleteNamespacedPod.mockResolvedValue({});
      const helper = new KubernetesHelper('test-ns');
      await helper.deletePod('target-pod');

      expect(mockCoreApi.deleteNamespacedPod).toHaveBeenCalledWith({
        name: 'target-pod',
        namespace: 'test-ns',
        gracePeriodSeconds: 0,
      });
    });
  });

  describe('waitForPodReady', () => {
    it('resolves when expected pods are ready', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          { metadata: { name: 'p1' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
          { metadata: { name: 'p2' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
        ],
      });

      const helper = new KubernetesHelper('test-ns');
      await expect(helper.waitForPodReady('app=test', 2, 5000)).resolves.toBeUndefined();
    });

    it('throws on timeout', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          { metadata: { name: 'p1' }, status: { phase: 'Running', containerStatuses: [{ ready: false }] }, spec: {} },
        ],
      });

      const helper = new KubernetesHelper('test-ns');
      await expect(helper.waitForPodReady('app=test', 3, 100)).rejects.toThrow('Timeout');
    });
  });

  describe('simulatePodRestart', () => {
    it('deletes first pod and returns its name', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({
        items: [
          { metadata: { name: 'pod-abc' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
          { metadata: { name: 'pod-def' }, status: { phase: 'Running', containerStatuses: [{ ready: true }] }, spec: {} },
        ],
      });
      mockCoreApi.deleteNamespacedPod.mockResolvedValue({});

      const helper = new KubernetesHelper('test-ns');
      const name = await helper.simulatePodRestart('app=test');

      expect(name).toBe('pod-abc');
      expect(mockCoreApi.deleteNamespacedPod).toHaveBeenCalledWith({
        name: 'pod-abc',
        namespace: 'test-ns',
        gracePeriodSeconds: 0,
      });
    });

    it('throws when no pods found', async () => {
      mockCoreApi.listNamespacedPod.mockResolvedValue({ items: [] });
      const helper = new KubernetesHelper('test-ns');
      await expect(helper.simulatePodRestart('app=missing')).rejects.toThrow('No pods found');
    });
  });

  describe('createK8sHelper', () => {
    it('creates a KubernetesHelper instance', () => {
      const helper = createK8sHelper('my-ns');
      expect(helper).toBeInstanceOf(KubernetesHelper);
    });
  });
});
