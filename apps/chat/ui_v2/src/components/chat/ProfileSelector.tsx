'use client';

import { useEffect } from 'react';
import { useChatStore } from '@/stores/chatStore';
import { useLocale } from '@/i18n/LocaleProvider';
import { template } from '@/config/template';

export function ProfileSelector() {
  return template.features.chat && template.features.profiles ? <EnabledProfileSelector /> : null;
}

function EnabledProfileSelector() {
  const { locale, t } = useLocale();
  const { 
    profiles, 
    selectedProfileId, 
    profileLocked, 
    loadProfiles, 
    setSelectedProfile 
  } = useChatStore();

  useEffect(() => {
    loadProfiles();
  }, [loadProfiles]);

  if (profiles.length === 0) {
    return null;
  }

  return (
    <div className="flex min-w-0 items-center gap-2">
      <select
        value={profiles.some((profile) => profile.id === selectedProfileId) ? selectedProfileId : ''}
        onChange={(e) => setSelectedProfile(e.target.value)}
        disabled={profileLocked}
        aria-label={t('Profile')}
        className={`
          min-w-0 max-w-44 text-sm rounded-lg px-2 py-1
          border border-white/25
          bg-slate-900 text-white
          focus:ring-2 focus:ring-white/60
          ${profileLocked 
            ? 'opacity-60 cursor-not-allowed bg-gray-800' 
            : 'cursor-pointer hover:border-gray-500'
          }
        `}
      >
        {!profiles.some((profile) => profile.id === selectedProfileId) && <option value="" disabled>{t('Profile')}</option>}
        {profiles.map((profile) => (
          <option key={profile.id} value={profile.id}>
            {profile.id === 'no_profile' ? t('Guest') : locale === 'ar' && profile.display_name_ar ? profile.display_name_ar : profile.display_name}
          </option>
        ))}
      </select>
      {profileLocked && (
        <div className="flex items-center gap-1 text-xs text-gray-500">
          <span>🔒</span>
          <span>{t('Locked')}</span>
        </div>
      )}
    </div>
  );
}
