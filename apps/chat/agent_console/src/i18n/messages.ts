import type { Locale } from '@/config/template';

const arabic = {
  'Agent console': 'وحدة تحكم الموظف',
  'Change language': 'تغيير اللغة',
  'Chat is not enabled.': 'المحادثة غير مفعّلة.',
  'Agent console is not enabled.': 'وحدة تحكم الموظف غير مفعّلة.',
  'Chat service is not configured.': 'لم يتم إعداد خدمة المحادثة.',
  'Please enter both Agent ID and Name': 'يرجى إدخال معرّف الموظف والاسم',
  'Login failed. Please try again.': 'تعذر تسجيل الدخول. يرجى المحاولة مرة أخرى.',
  'Agent ID': 'معرّف الموظف',
  'Your name': 'اسمك',
  'Enter your agent ID': 'أدخل معرّف الموظف',
  'Enter your name': 'أدخل اسمك',
  'Signing in…': 'جارٍ تسجيل الدخول…',
  'Enter console': 'دخول وحدة التحكم',
  'Agent support': 'دعم الموظفين',
  'Agent': 'الموظف',
  'Local': 'التوقيت المحلي',
  'Online': 'متصل',
  'Offline': 'غير متصل',
  'Available': 'متاح',
  'Sign out': 'تسجيل الخروج',
  'No active conversation': 'لا توجد محادثة نشطة',
  'Accept a conversation from the queue to begin.': 'اقبل محادثة من قائمة الانتظار للبدء.',
  'Waiting queue': 'قائمة الانتظار',
  'All clear': 'قائمة الانتظار فارغة',
  'No conversations are waiting.': 'لا توجد محادثات في الانتظار.',
  'Busy': 'مشغول',
  'Accept handover': 'قبول التحويل',
  'Average wait': 'متوسط الانتظار',
  'Conversation #{id}': 'المحادثة #{id}',
  'Live for {minutes}m {seconds}s': 'مدة المحادثة {minutes} د {seconds} ث',
  'End this handover?': 'هل تريد إنهاء هذا التحويل؟',
  'Cancel': 'إلغاء',
  'Confirm': 'تأكيد',
  'Complete handover': 'إنهاء التحويل',
  "You're connected": 'أنت متصل',
  'Send a message to begin.': 'أرسل رسالة للبدء.',
  'Conversation with {name}': 'المحادثة مع {name}',
  'Before handover': 'قبل التحويل',
  'You joined': 'لقد انضممت',
  'Type a reply…': 'اكتب رداً…',
  'Send message': 'إرسال الرسالة',
  'Suggested replies': 'ردود مقترحة',
  '{seconds}s': '{seconds} ث',
  '{minutes}m {seconds}s': '{minutes} د {seconds} ث',
} as const;

export type MessageKey = keyof typeof arabic;

export function translate(locale: Locale, key: MessageKey, values: Record<string, string | number> = {}) {
  let text: string = locale === 'ar' ? arabic[key] : key;
  for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, String(value));
  return text;
}
