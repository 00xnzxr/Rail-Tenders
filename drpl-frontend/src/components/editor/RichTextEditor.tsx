import { useEffect, useRef, useState } from 'react';
import { useEditor, EditorContent, Extension } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import Underline from '@tiptap/extension-underline';
import TextAlign from '@tiptap/extension-text-align';
import FontFamily from '@tiptap/extension-font-family';
import { TextStyle } from '@tiptap/extension-text-style';
import Color from '@tiptap/extension-color';
import Highlight from '@tiptap/extension-highlight';
import { Table } from '@tiptap/extension-table';
import TableRow from '@tiptap/extension-table-row';
import TableHeader from '@tiptap/extension-table-header';
import TableCell from '@tiptap/extension-table-cell';
import Link from '@tiptap/extension-link';
import { Image as TipTapImage } from '@tiptap/extension-image';
import {
  Bold, Italic, Underline as UnderlineIcon, Strikethrough,
  AlignLeft, AlignCenter, AlignRight, AlignJustify,
  List, ListOrdered, Undo, Redo, Minus, Table as TableIcon,
  Highlighter, Link as LinkIcon, PenTool, ChevronDown, Loader2,
} from 'lucide-react';

// Custom FontSize extension via TextStyle attribute
const FontSize = Extension.create({
  name: 'fontSize',
  addGlobalAttributes() {
    return [
      {
        types: ['textStyle'],
        attributes: {
          fontSize: {
            default: null,
            parseHTML: (element) => element.style.fontSize?.replace('pt', '') || null,
            renderHTML: (attributes) => {
              if (!attributes.fontSize) return {};
              return { style: `font-size: ${attributes.fontSize}pt` };
            },
          },
        },
      },
    ];
  },
  addCommands() {
    return {
      setFontSize:
        (size: string) =>
        ({ chain }: any) => {
          return chain().setMark('textStyle', { fontSize: size }).run();
        },
      unsetFontSize:
        () =>
        ({ chain }: any) => {
          return chain().setMark('textStyle', { fontSize: null }).run();
        },
    } as any;
  },
});

export interface EditorSignatureItem {
  id: number;
  name: string;
  designation?: string | null;
  has_signature_image?: boolean;
  has_stamp_image?: boolean;
}

interface RichTextEditorProps {
  value: string;
  onChange: (html: string) => void;
  placeholder?: string;
  minHeight?: number;
  /**
   * When provided AND non-empty, the toolbar shows a "Sign" dropdown that
   * inserts a chosen signature image inline at the cursor position. Replaces
   * the legacy coordinate-based placement board with Word-style picture
   * insertion.
   */
  signatures?: EditorSignatureItem[];
  /**
   * Resolves a signature pick to a data URI (the parent fetches the bytes
   * and base64-encodes them — typically via getSignatureImageDataUri).
   * Returns the data URI string. The editor inserts the image at the
   * current selection.
   */
  onResolveSignature?: (
    signatureId: number,
    kind: 'signature' | 'stamp',
  ) => Promise<{ dataUri: string; name: string; designation?: string | null }>;
  /**
   * Fires after a signature image has been successfully inserted into the
   * editor. Parent typically uses this to (a) flush the auto-save debounce
   * so the data URI gets persisted immediately, and (b) refresh the PDF
   * preview pane so the inserted image shows up without the user having
   * to click Refresh.
   */
  onSignatureInserted?: () => void;
}

const FONT_FAMILIES = [
  { label: 'Times New Roman', value: 'Times New Roman, serif' },
  { label: 'Arial', value: 'Arial, sans-serif' },
  { label: 'Calibri', value: 'Calibri, sans-serif' },
  { label: 'Courier New', value: 'Courier New, monospace' },
  { label: 'Georgia', value: 'Georgia, serif' },
];

const FONT_SIZES = ['8', '9', '10', '11', '12', '14', '16', '18', '20', '24', '28', '36', '48'];

const HEADING_OPTIONS = [
  { label: 'Paragraph', value: 'paragraph' },
  { label: 'Heading 1', value: 'h1' },
  { label: 'Heading 2', value: 'h2' },
  { label: 'Heading 3', value: 'h3' },
  { label: 'Heading 4', value: 'h4' },
];

function ToolbarBtn({
  onClick, active, title, children, disabled,
}: {
  onClick: () => void;
  active?: boolean;
  title: string;
  children: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onMouseDown={(e) => { e.preventDefault(); onClick(); }}
      title={title}
      disabled={disabled}
      className={`p-1 rounded transition-colors ${
        active ? 'bg-muted text-foreground' : 'text-muted-foreground hover:bg-muted'
      } ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <div className="w-px h-5 bg-muted mx-0.5" />;
}

export default function RichTextEditor({
  value,
  onChange,
  placeholder,
  minHeight = 480,
  signatures,
  onResolveSignature,
  onSignatureInserted,
}: RichTextEditorProps) {
  const isExternalUpdate = useRef(false);
  const [signatureMenuOpen, setSignatureMenuOpen] = useState(false);
  const [insertingSigId, setInsertingSigId] = useState<number | null>(null);
  const signatureMenuRef = useRef<HTMLDivElement>(null);

  // Close the signature menu on outside click.
  useEffect(() => {
    if (!signatureMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (
        signatureMenuRef.current &&
        !signatureMenuRef.current.contains(e.target as Node)
      ) {
        setSignatureMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [signatureMenuOpen]);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({ heading: { levels: [1, 2, 3, 4] } }),
      Underline,
      TextAlign.configure({ types: ['heading', 'paragraph'] }),
      FontFamily,
      TextStyle,
      FontSize,
      Color,
      Highlight.configure({ multicolor: true }),
      Table.configure({ resizable: false }),
      TableRow,
      TableHeader,
      TableCell,
      Link.configure({ openOnClick: false }),
      // Inline picture support for the signature-insert flow. inline=true so
      // the image can sit alongside text (e.g. "Signed: <img> Director").
      // allowBase64=true is critical — the signature data arrives as a
      // base64 data URI from the backend and TipTap defaults to rejecting
      // those.
      TipTapImage.configure({
        inline: true,
        allowBase64: true,
        HTMLAttributes: {
          class: 'editor-signature-image',
          style: 'max-width: 200px; height: auto; vertical-align: middle;',
        },
      }),
    ],
    content: value,
    editorProps: {
      attributes: {
        class: 'outline-none',
        style: `min-height: ${minHeight}px; padding: 12px 16px; font-family: Times New Roman, serif; font-size: 12pt; line-height: 1.6;`,
        'data-placeholder': placeholder || 'Start typing your document...',
      },
    },
    onUpdate({ editor: e }) {
      if (!isExternalUpdate.current) {
        onChange(e.getHTML());
      }
    },
  });

  // Sync external value changes to editor without triggering onChange
  useEffect(() => {
    if (!editor) return;
    const current = editor.getHTML();
    if (value !== current) {
      isExternalUpdate.current = true;
      editor.commands.setContent(value, { emitUpdate: false });
      isExternalUpdate.current = false;
    }
  }, [value, editor]);

  if (!editor) return null;

  const getActiveHeading = () => {
    for (let i = 1; i <= 4; i++) {
      if (editor.isActive('heading', { level: i })) return `h${i}`;
    }
    return 'paragraph';
  };

  const setHeading = (val: string) => {
    if (val === 'paragraph') {
      editor.chain().focus().setParagraph().run();
    } else {
      const level = parseInt(val.replace('h', '')) as 1 | 2 | 3 | 4;
      editor.chain().focus().toggleHeading({ level }).run();
    }
  };

  const getActiveFontFamily = () => {
    const mark = editor.getAttributes('textStyle');
    return mark?.fontFamily || '';
  };

  const getActiveFontSize = () => {
    const mark = editor.getAttributes('textStyle');
    return mark?.fontSize || '12';
  };

  const handleInsertLink = () => {
    const prev = editor.getAttributes('link').href || '';
    const url = window.prompt('Enter URL:', prev);
    if (url === null) return;
    if (url === '') {
      editor.chain().focus().extendMarkRange('link').unsetLink().run();
    } else {
      editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run();
    }
  };

  const handleInsertTable = () => {
    editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run();
  };

  const handleInsertSignature = async (sig: EditorSignatureItem) => {
    if (!onResolveSignature) return;
    setInsertingSigId(sig.id);
    try {
      // Most user-named "Stamp"/"Stamp 2" records only have stamp_image_path;
      // ask for that explicitly when the signature image is unavailable so
      // the backend doesn't have to guess. The backend already falls back
      // either way, but this keeps the intent explicit.
      const kind: 'signature' | 'stamp' =
        sig.has_signature_image === false && sig.has_stamp_image ? 'stamp' : 'signature';
      const { dataUri, name } = await onResolveSignature(sig.id, kind);
      editor
        .chain()
        .focus()
        .setImage({ src: dataUri, alt: name } as any)
        .run();
      setSignatureMenuOpen(false);
      // Let the parent flush the auto-save debounce + refresh the PDF
      // preview so the inserted signature shows up immediately. Without
      // this, the preview only auto-refreshes when letterhead / orientation
      // change — meaning the user has to manually click Refresh.
      onSignatureInserted?.();
    } catch {
      // Surface upstream — parent's catch handles toast/error UI.
    } finally {
      setInsertingSigId(null);
    }
  };

  return (
    <div className="border border-border rounded-lg overflow-hidden focus-within:ring-2 focus-within:ring-drpl-secondary focus-within:border-drpl-secondary transition-shadow">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-0.5 px-2 py-1.5 bg-muted/40 border-b border-border">

        {/* History */}
        <ToolbarBtn onClick={() => editor.chain().focus().undo().run()} title="Undo" disabled={!editor.can().undo()}>
          <Undo size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().redo().run()} title="Redo" disabled={!editor.can().redo()}>
          <Redo size={14} />
        </ToolbarBtn>

        <Divider />

        {/* Block type */}
        <select
          value={getActiveHeading()}
          onChange={(e) => setHeading(e.target.value)}
          onMouseDown={(e) => e.stopPropagation()}
          className="text-xs border border-border rounded px-1 py-0.5 bg-card focus:outline-none h-6"
          title="Block type"
        >
          {HEADING_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>

        <Divider />

        {/* Font family */}
        <select
          value={getActiveFontFamily()}
          onChange={(e) => editor.chain().focus().setFontFamily(e.target.value).run()}
          onMouseDown={(e) => e.stopPropagation()}
          className="text-xs border border-border rounded px-1 py-0.5 bg-card focus:outline-none h-6 max-w-[120px]"
          title="Font family"
        >
          <option value="">Font</option>
          {FONT_FAMILIES.map((f) => (
            <option key={f.value} value={f.value} style={{ fontFamily: f.value }}>{f.label}</option>
          ))}
        </select>

        {/* Font size */}
        <select
          value={getActiveFontSize()}
          onChange={(e) => (editor.chain().focus() as any).setFontSize(e.target.value).run()}
          onMouseDown={(e) => e.stopPropagation()}
          className="text-xs border border-border rounded px-1 py-0.5 bg-card focus:outline-none h-6 w-14"
          title="Font size"
        >
          {FONT_SIZES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>

        <Divider />

        {/* Inline marks */}
        <ToolbarBtn onClick={() => editor.chain().focus().toggleBold().run()} active={editor.isActive('bold')} title="Bold">
          <Bold size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().toggleItalic().run()} active={editor.isActive('italic')} title="Italic">
          <Italic size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().toggleUnderline().run()} active={editor.isActive('underline')} title="Underline">
          <UnderlineIcon size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().toggleStrike().run()} active={editor.isActive('strike')} title="Strikethrough">
          <Strikethrough size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().toggleHighlight().run()} active={editor.isActive('highlight')} title="Highlight">
          <Highlighter size={14} />
        </ToolbarBtn>

        {/* Text color */}
        <label title="Text color" className="relative p-1 rounded cursor-pointer hover:bg-muted transition-colors flex items-center">
          <span className="text-xs font-bold" style={{ color: editor.getAttributes('textStyle').color || '#333' }}>A</span>
          <input
            type="color"
            className="absolute inset-0 opacity-0 cursor-pointer w-full h-full"
            value={editor.getAttributes('textStyle').color || '#000000'}
            onChange={(e) => editor.chain().focus().setColor(e.target.value).run()}
          />
        </label>

        <Divider />

        {/* Alignment */}
        <ToolbarBtn onClick={() => editor.chain().focus().setTextAlign('left').run()} active={editor.isActive({ textAlign: 'left' })} title="Align left">
          <AlignLeft size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().setTextAlign('center').run()} active={editor.isActive({ textAlign: 'center' })} title="Align center">
          <AlignCenter size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().setTextAlign('right').run()} active={editor.isActive({ textAlign: 'right' })} title="Align right">
          <AlignRight size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().setTextAlign('justify').run()} active={editor.isActive({ textAlign: 'justify' })} title="Justify">
          <AlignJustify size={14} />
        </ToolbarBtn>

        <Divider />

        {/* Lists */}
        <ToolbarBtn onClick={() => editor.chain().focus().toggleBulletList().run()} active={editor.isActive('bulletList')} title="Bullet list">
          <List size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().toggleOrderedList().run()} active={editor.isActive('orderedList')} title="Numbered list">
          <ListOrdered size={14} />
        </ToolbarBtn>

        <Divider />

        {/* Insert */}
        <ToolbarBtn onClick={handleInsertTable} title="Insert table" active={editor.isActive('table')}>
          <TableIcon size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={() => editor.chain().focus().setHorizontalRule().run()} title="Horizontal rule">
          <Minus size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={handleInsertLink} active={editor.isActive('link')} title="Link">
          <LinkIcon size={14} />
        </ToolbarBtn>

        {/* Insert Signature — Word-style inline image. Only rendered when the
            parent has supplied a signature list + a resolver. */}
        {signatures && signatures.length > 0 && onResolveSignature && (
          <>
            <Divider />
            <div ref={signatureMenuRef} className="relative">
              <button
                type="button"
                onClick={() => setSignatureMenuOpen((v) => !v)}
                title="Insert signature at cursor"
                className={`inline-flex items-center gap-0.5 h-6 px-2 rounded text-xs transition-colors ${
                  signatureMenuOpen
                    ? 'bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400'
                    : 'text-foreground hover:bg-muted'
                }`}
              >
                <PenTool size={13} />
                <span className="hidden sm:inline ml-1">Sign</span>
                <ChevronDown size={11} />
              </button>
              {signatureMenuOpen && (
                <div className="absolute z-30 mt-1 left-0 w-56 bg-card border border-border rounded-lg shadow-lg py-1 max-h-72 overflow-y-auto">
                  <p className="px-3 py-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                    Insert at cursor
                  </p>
                  {signatures.map((sig) => {
                    const disabled = !sig.has_signature_image && !sig.has_stamp_image;
                    return (
                      <button
                        key={sig.id}
                        type="button"
                        disabled={disabled || insertingSigId !== null}
                        onClick={() => handleInsertSignature(sig)}
                        className="w-full text-left px-3 py-1.5 text-xs hover:bg-muted/40 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-between gap-2"
                      >
                        <span className="flex flex-col min-w-0">
                          <span className="font-medium text-foreground truncate">
                            {sig.name}
                          </span>
                          {sig.designation && (
                            <span className="text-[10px] text-muted-foreground truncate">
                              {sig.designation}
                            </span>
                          )}
                        </span>
                        {insertingSigId === sig.id ? (
                          <Loader2 size={12} className="animate-spin text-muted-foreground flex-shrink-0" />
                        ) : disabled ? (
                          <span className="text-[9px] text-muted-foreground">no image</span>
                        ) : null}
                      </button>
                    );
                  })}
                  {signatures.length === 0 && (
                    <p className="px-3 py-2 text-xs text-muted-foreground">
                      No signatures available. Add one in Settings.
                    </p>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* Editor area */}
      <div className="bg-card overflow-x-auto">
        <style>{`
          .ProseMirror p.is-empty:first-child::before {
            content: attr(data-placeholder);
            float: left;
            color: #adb5bd;
            pointer-events: none;
            height: 0;
          }
          /* Wide tables render at their natural width; the editor's outer
             wrapper has overflow-x:auto so the user gets a horizontal
             scrollbar instead of the browser squeezing columns letter-by-letter. */
          .ProseMirror table {
            border-collapse: collapse;
            width: max-content;
            margin: 8px 0;
            table-layout: auto;
          }
          .ProseMirror th, .ProseMirror td {
            border: 1px solid #ddd;
            padding: 6px 8px;
            text-align: left;
            vertical-align: top;
            word-break: normal;
            overflow-wrap: normal;
            min-width: 80px;
            white-space: normal;
          }
          .ProseMirror th {
            background: #f5f5f5;
            font-weight: bold;
          }
          .ProseMirror a {
            color: #2563eb;
            text-decoration: underline;
          }
          .ProseMirror h1 { font-size: 1.75em; font-weight: bold; margin: 0.5em 0; }
          .ProseMirror h2 { font-size: 1.5em; font-weight: bold; margin: 0.5em 0; }
          .ProseMirror h3 { font-size: 1.25em; font-weight: bold; margin: 0.5em 0; }
          .ProseMirror h4 { font-size: 1.1em; font-weight: bold; margin: 0.5em 0; }
          .ProseMirror ul { list-style-type: disc; padding-left: 1.5em; }
          .ProseMirror ol { list-style-type: decimal; padding-left: 1.5em; }
          .ProseMirror blockquote { border-left: 3px solid #ddd; padding-left: 1em; color: #666; margin: 0.5em 0; }
          .ProseMirror hr { border: none; border-top: 2px solid #ddd; margin: 1em 0; }
          .ProseMirror mark { background-color: #fef08a; padding: 0 2px; }

          /* Inline signature images — make drag affordance obvious and
             provide visual feedback on hover and when the image is the
             current ProseMirror selection. Users can click+hold and drag
             the image to a different position within the document. */
          .ProseMirror img.editor-signature-image {
            cursor: grab;
            border: 2px solid transparent;
            border-radius: 4px;
            transition: border-color 0.15s ease;
            display: inline-block;
          }
          .ProseMirror img.editor-signature-image:hover {
            border-color: rgba(99, 102, 241, 0.35);
          }
          .ProseMirror img.editor-signature-image.ProseMirror-selectednode {
            border-color: rgb(99, 102, 241);
            outline: 2px solid rgba(99, 102, 241, 0.2);
            outline-offset: 1px;
            cursor: grabbing;
          }
        `}</style>
        <EditorContent editor={editor} />
      </div>
    </div>
  );
}
