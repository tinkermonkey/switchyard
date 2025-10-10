/**
 * HeaderStatsCard - Standard stats card for header (Total Events, Tokens, etc.)
 */
import HeaderBox from './HeaderBox'

export default function HeaderStatsCard({ title, value }) {
  return (
    <HeaderBox title={title}>
      <div className="text-xl font-semibold text-gh-accent-primary">
        {value}
      </div>
    </HeaderBox>
  )
}
