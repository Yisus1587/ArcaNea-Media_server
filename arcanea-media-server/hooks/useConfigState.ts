import { useState } from 'react';
import { MetadataConfig } from '../types';

export const useConfigState = (defaultProviderLanguage: string) => {
  const [metadataConfig, setMetadataConfig] = useState<MetadataConfig>({
    moviesProvider: 'tmdb',
    animeProvider: 'kitsu',
    language: defaultProviderLanguage,
    downloadImages: true,
    fetchCast: true,
  });
  const [libraryPaths, setLibraryPaths] = useState<string[]>([]);
  const [looseMovieRoots, setLooseMovieRoots] = useState<string[]>([]);

  return {
    metadataConfig,
    setMetadataConfig,
    libraryPaths,
    setLibraryPaths,
    looseMovieRoots,
    setLooseMovieRoots,
  };
};
