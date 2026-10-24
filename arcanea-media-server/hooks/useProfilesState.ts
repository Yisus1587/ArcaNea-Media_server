import { useEffect, useState } from 'react';
import { AppStage, MetadataConfig, UserProfile } from '../types';
import { MediaService } from '../services/api';

interface UseProfilesStateParams {
  appStage: AppStage;
  metadataConfig: MetadataConfig;
  libraryPaths: string[];
  looseMovieRoots: string[];
  currentUser: UserProfile | null;
  setCurrentUser: (u: UserProfile | null) => void;
  onLogout: () => void;
  showNotification: (type: 'success' | 'error' | 'info', message: string) => void;
}

export const useProfilesState = ({
  appStage,
  metadataConfig,
  libraryPaths,
  looseMovieRoots,
  currentUser,
  setCurrentUser,
  onLogout,
  showNotification,
}: UseProfilesStateParams) => {
  const [profiles, setProfiles] = useState<UserProfile[]>([]);

  const handleAddProfile = (newProfile: Omit<UserProfile, 'id'>) => {
    const rid =
      globalThis.crypto && 'randomUUID' in globalThis.crypto
        ? (globalThis.crypto as any).randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const safe: any = { ...(newProfile as any) };
    if (safe.isKid) safe.isManager = false;
    const profile = { ...safe, id: String(rid) } as UserProfile;
    setProfiles((prev) => [...prev, profile]);
    showNotification('success', 'Perfil creado correctamente');
  };

  const handleUpdateProfile = (updatedProfile: UserProfile) => {
    setProfiles((prev) => prev.map((p) => (p.id === updatedProfile.id ? updatedProfile : p)));
    showNotification('success', 'Perfil actualizado');
    if (currentUser?.id === updatedProfile.id) {
      setCurrentUser(updatedProfile);
    }
  };

  const handleDeleteProfile = (id: string) => {
    if (profiles.length <= 1) {
      showNotification('error', 'No se puede eliminar el último perfil');
      return;
    }

    const target = profiles.find((p) => p.id === id);
    if (target?.isManager) {
      const managers = profiles.filter((p) => !!p.isManager);
      if (managers.length <= 1) {
        showNotification('error', 'No se puede eliminar el último perfil de gestión');
        return;
      }
    }

    if (currentUser?.id === id) {
      setProfiles((prev) => prev.filter((p) => p.id !== id));
      onLogout();
      showNotification('info', 'Se eliminó el perfil activo');
    } else {
      setProfiles((prev) => prev.filter((p) => p.id !== id));
      showNotification('info', 'Perfil eliminado');
    }
  };

  useEffect(() => {
    if (appStage !== 'app') return;
    if (!profiles || profiles.length === 0) return;
    const t = window.setTimeout(() => {
      (async () => {
        try {
          await MediaService.saveAppConfig({
            setupComplete: true,
            profiles,
            metadata: metadataConfig,
            media_roots: libraryPaths,
            loose_movie_roots: looseMovieRoots,
          });
        } catch {
          // ignore
        }
      })();
    }, 600);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profiles]);

  return {
    profiles,
    setProfiles,
    handleAddProfile,
    handleUpdateProfile,
    handleDeleteProfile,
  };
};
